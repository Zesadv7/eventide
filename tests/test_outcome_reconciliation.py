"""Only proven builtin file states may close unknown outcomes; writes are never replayed."""

import asyncio
import subprocess

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.budgets import BudgetScope
from eventide.executor import ToolContext, ToolExecutor
from eventide.file_operations import FileOperations
from eventide.host import RuntimeHost
from eventide.model_budget import ModelBudgetController
from eventide.models import RunRequest, ToolCall
from eventide.outcome_reconciliation import unresolved_outcomes
from eventide.providers import ScriptedProvider
from eventide.workspace import digest
from tests.test_api import wait_for_run
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def prepare_repo(path):
    repo = make_repo(path / "repo")
    (repo / "code.py").write_text("old\n")
    (repo / "other.py").write_text("unchanged\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True
    )
    return repo


async def lost_file_result(path, *, target="source", tool="write_file", terminal=False):
    repo = prepare_repo(path)
    settings = settings_for(repo, state_dir=path / "state")
    host = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    session = host.create_session()
    run_id = await host.admit_run(RunRequest("repair code", session, execution_target=target))
    binding = host._execution_binding(run_id)
    root, cwd = host._execution_paths(binding) if binding else (repo, repo)
    workspace_id = (
        binding["execution_workspace_id"] if binding else host.default_workspace.workspace_id
    )
    args = (
        {"path": "code.py", "content": "new\n"}
        if tool == "write_file"
        else {"path": "code.py", "old_text": "old", "new_text": "new"}
    )
    host.store.append_event(
        run_id,
        "run.started",
        {
            "session_id": session,
            "workspace_id": host.default_workspace.workspace_id,
            "provider": "scripted",
            "model": "scripted",
            "mode": "auto",
        },
    )
    host.store.append_event(
        run_id,
        "model.response",
        {
            "step": 1,
            "text": "",
            "stop_reason": "tool_use",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "lost", "name": tool, "input": args},
                ],
            },
        },
    )
    host.store.append_event(
        run_id,
        "tool.prepared",
        {
            "call_id": "lost",
            "name": tool,
            "arguments": args,
            "arguments_fingerprint": digest(args),
            "readonly": False,
        },
    )
    executor = ToolExecutor(
        host.handlers,
        tools=host.tools,
        file_operations=host.file_operations,
        workspace_id=workspace_id,
    )
    result = await executor.execute(
        ToolCall("lost", tool, args), ToolContext(cwd, run_id, host.policy)
    )
    assert not result.is_error, result.content
    record = host.file_operations.get(session, result.metadata["file_operation_id"])
    if terminal:
        host.store.append_event(
            run_id,
            "tool.outcome_unknown",
            {
                "call_id": "lost",
                "name": tool,
                "workspace_id": workspace_id,
                "readonly": False,
                "error": "completion record unavailable",
            },
        )
        host.store.append_event(run_id, "run.interrupted", {"reason": "unknown_tool_outcome"})
        host._release_admission(run_id)
        host.dispatch.finished(run_id)
    return host, settings, session, run_id, root, record


def crash(host):
    host._lease.close()
    host.store.close()


async def wait_terminal(host, run_id):
    async def wait():
        while not host.store.run_is_terminal(run_id):
            await asyncio.sleep(0.01)
        return host.store.get_run(run_id)

    return await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize("target", ["source", "worktree"])
@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
async def test_crash_result_is_verified_without_replaying_original_write(
    isolated_workspace,
    monkeypatch,
    target,
    tool,
):
    writes = []
    original_write = FileOperations._write_file

    def count_write(self, *args, **kwargs):
        writes.append(args[1])
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(FileOperations, "_write_file", count_write)
    host, settings, session, old, root, file_record = await lost_file_result(
        isolated_workspace,
        target=target,
        tool=tool,
    )
    crash(host)
    provider = ScriptedProvider([{"text": "continued after verified file state"}])
    restarted = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        await restarted.initialize()
        claims = [e for e in restarted.store.run_events(old) if e["type"] == "recovery.claimed"]
        assert len(claims) == 1
        successor = claims[0]["payload"]["resumed_as"]
        assert (await wait_terminal(restarted, successor))["status"] == "completed"
        checks = [
            e
            for e in restarted.store.run_outcome_events(old)
            if e["type"] == "tool.outcome_reconciled"
        ]
        assert len(checks) == 1 and checks[0]["payload"]["status"] == "verified_after"
        assert checks[0]["run_id"] is None
        completed = [
            e for e in restarted.store.run_events(successor) if e["type"] == "tool.completed"
        ]
        assert len(completed) == 1 and completed[0]["payload"]["call_id"] == "lost"
        assert completed[0]["payload"]["metadata"]["outcome_reconciled"] is True
        assert not unresolved_outcomes(restarted.store.run_outcome_events(old))
        assert not restarted.store.workspace_has_unknown_outcome(file_record["workspace_id"])
        assert len(writes) == 1 and (root / "code.py").read_text() == "new\n"
        if target == "worktree":
            assert (settings.workdir / "code.py").read_text() == "old\n"
        _, budget = ModelBudgetController(restarted.store).context(successor)
        assert budget.root_run_id == old
        request = provider.requests[0]
        assert any(
            block.get("tool_use_id") == "lost"
            for msg in request.messages
            for block in msg["content"]
            if isinstance(msg["content"], list)
        )
        await restarted.initialize()
        assert len(provider.requests) == 1
    finally:
        await restarted.close()
    again = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        assert len(again.store.facts_by_type(("recovery.claimed",))) == 1
        assert len(again.store.facts_by_type(("tool.outcome_reconciled",))) == 1
    finally:
        await again.close()


@pytest.mark.parametrize(
    "change,reason",
    [
        ("before", "current_matches_before"),
        ("third", "file_state_changed"),
        ("other", "workspace_context_changed"),
        ("legacy", "missing_prepared_workspace_evidence"),
    ],
)
async def test_insufficient_evidence_stays_unknown(isolated_workspace, change, reason):
    host, settings, session, old, root, record = await lost_file_result(isolated_workspace)
    if change == "before":
        (root / "code.py").write_text("old\n")
    elif change == "third":
        (root / "code.py").write_text("different\n")
    elif change == "other":
        (root / "other.py").write_text("user changed another file\n")
    else:
        legacy = {k: v for k, v in record.items() if k != "recovery_context"}
        host.store.append_fact(session, "file.operation", legacy)
    crash(host)
    provider = ScriptedProvider([])
    restarted = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        await restarted.initialize()
        checks = [
            e["payload"]
            for e in restarted.store.run_outcome_events(old)
            if e["type"] == "tool.outcome_reconciled"
        ]
        assert len(checks) == 1 and checks[0]["status"] == "requires_review"
        assert checks[0]["reason"] == reason
        assert restarted.store.workspace_has_unknown_outcome(
            restarted.default_workspace.workspace_id
        )
        assert not restarted.store.facts_by_type(("recovery.claimed",))
        assert provider.requests == []
        with pytest.raises(ValueError, match="Unknown|unresolved|Workspace"):
            await restarted.continue_session(session)
    finally:
        await restarted.close()


@pytest.mark.parametrize("stop", ["user", "approval", "budget"])
async def test_verified_file_state_does_not_bypass_stop_approval_or_budget(
    isolated_workspace, stop
):
    host, settings, session, old, _, _ = await lost_file_result(isolated_workspace)
    if stop == "user":
        host.store.append_fact(session, "run.cancel_requested", {"run_id": old}, author="user")
    elif stop == "approval":
        host.store.append_event(
            old, "approval.required", {"approval_id": "pending", "summary": "danger"}
        )
    else:
        host.configure_budget(scope=BudgetScope("run", old), token_limit=1, request_id="budget")
    crash(host)
    provider = ScriptedProvider([])
    restarted = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        await restarted.initialize()
        claims = restarted.store.facts_by_type(("recovery.claimed",))
        if stop == "budget":
            assert len(claims) == 1
            successor = claims[0]["payload"]["resumed_as"]
            assert (await wait_terminal(restarted, successor))["status"] == "interrupted"
            assert any(e["type"] == "budget.blocked" for e in restarted.store.run_events(successor))
        else:
            assert claims == []
        assert provider.requests == []
    finally:
        await restarted.close()


async def test_terminal_check_is_idempotent_and_does_not_automatically_resume(isolated_workspace):
    host, _, session, old, root, _ = await lost_file_result(isolated_workspace, terminal=True)
    try:
        before = host.store.run_events(old)
        first = await host.reconcile_run_outcomes(old)
        second = await host.reconcile_run_outcomes(old)
        assert first == second and first["unresolved"] == 0
        assert len(host.store.facts_by_type(("tool.outcome_reconciled",))) == 1
        assert host.store.run_events(old) == before
        assert host.store.latest_run(session)["id"] == old
        assert host.provider.requests == []
        assert (root / "code.py").read_text() == "new\n"
        (root / "code.py").write_text("later user edit\n")
        with pytest.raises(ValueError, match="Workspace changed"):
            await host.continue_session(session)
    finally:
        await host.close()


def test_http_reconcile_shows_evidence_and_explicit_continue_pairs_original_call(
    isolated_workspace,
):
    host, _, session, old, _, _ = asyncio.run(lost_file_result(isolated_workspace, terminal=True))
    with TestClient(create_app(host)) as client:
        url = f"/api/runs/{old}/recovery/reconcile"
        denied = client.post(
            url, headers={"Origin": "https://foreign.invalid", "Idempotency-Key": "foreign"}
        )
        assert denied.status_code == 403
        before = host.store.run_events(old)
        response = client.post(url, headers={"Idempotency-Key": "check"})
        assert response.status_code == 200, response.text
        assert response.json()["unresolved"] == 0
        assert client.post(url, headers={"Idempotency-Key": "check"}).json() == response.json()
        assert host.store.run_events(old) == before
        view = client.get(f"/api/runs/{old}/recovery").json()
        assert view["unknown_tools"] == []
        assert view["outcome_checks"][0]["status"] == "verified_after"
        assert view["available_actions"]["continue"]["available"] is True
        accepted = client.post(
            f"/api/sessions/{session}/continue",
            json={"expected_run_id": old},
            headers={"Idempotency-Key": "continue"},
        )
        assert accepted.status_code == 202, accepted.text
        successor = accepted.json()["run_id"]
        assert wait_for_run(client, successor)["status"] == "completed"
        results = [e for e in host.store.run_events(successor) if e["type"] == "tool.completed"]
        assert len(results) == 1 and results[0]["payload"]["call_id"] == "lost"


@pytest.mark.parametrize("change", ["call", "directory"])
async def test_original_call_and_actual_execution_directory_must_still_match(
    isolated_workspace, change
):
    host, settings, _, old, root, _ = await lost_file_result(isolated_workspace)
    if change == "call":
        prepared = next(
            e["payload"] for e in host.store.run_events(old) if e["type"] == "tool.prepared"
        )
        host.store.append_event(
            old, "tool.prepared", {**prepared, "arguments_fingerprint": "other"}
        )
    else:
        moved = root.with_name("moved-repo")
        root.rename(moved)
        root.mkdir()
        (root / "code.py").write_text("new\n")
    crash(host)
    provider = ScriptedProvider([])
    restarted = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        await restarted.initialize()
        assert not restarted.store.facts_by_type(("recovery.claimed",))
        checks = [e["payload"] for e in restarted.store.facts_by_type(("tool.outcome_reconciled",))]
        assert checks[0]["status"] == "requires_review"
        assert checks[0]["reason"] == (
            "original_call_evidence_mismatch" if change == "call" else "evidence_unavailable"
        )
        assert provider.requests == []
    finally:
        await restarted.close()


async def test_service_close_waits_for_a_readonly_check_before_closing_store(
    isolated_workspace, monkeypatch
):
    import threading

    host, _, _, old, _, _ = await lost_file_result(isolated_workspace, terminal=True)
    entered, release = threading.Event(), threading.Event()
    original = host.file_operations.inspect_outcome

    def blocked(record):
        entered.set()
        assert release.wait(3)
        return original(record)

    monkeypatch.setattr(host.file_operations, "inspect_outcome", blocked)
    task = asyncio.create_task(host.reconcile_run_outcomes(old))
    assert await asyncio.to_thread(entered.wait, 3)
    closing = asyncio.create_task(host.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await closing


async def test_second_crash_after_verified_claim_keeps_one_result_and_successor(isolated_workspace):
    host, settings, _, old, _, _ = await lost_file_result(isolated_workspace)
    crash(host)
    first_restart = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    successor = first_restart.store.facts_by_type(("recovery.claimed",))[0]["payload"]["resumed_as"]
    crash(first_restart)
    provider = ScriptedProvider([{"text": "done"}])
    restarted = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        await restarted.initialize()
        assert (await wait_terminal(restarted, successor))["status"] == "completed"
        assert len(restarted.store.facts_by_type(("recovery.claimed",))) == 1
        assert len(restarted.store.facts_by_type(("tool.outcome_reconciled",))) == 1
        assert (
            len(
                [
                    event
                    for event in restarted.store.run_events(successor)
                    if event["type"] == "tool.completed"
                ]
            )
            == 1
        )
        assert provider.requests
        assert not unresolved_outcomes(restarted.store.run_outcome_events(old))
    finally:
        await restarted.close()


async def test_staging_the_target_after_the_write_is_not_excluded_from_proof(isolated_workspace):
    host, settings, _, old, root, _ = await lost_file_result(isolated_workspace)
    subprocess.run(["git", "-C", str(root), "add", "code.py"], check=True, capture_output=True)
    crash(host)
    restarted = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        await restarted.initialize()
        checks = restarted.store.facts_by_type(("tool.outcome_reconciled",))
        assert checks[0]["payload"]["reason"] == "workspace_context_changed"
        assert not restarted.store.facts_by_type(("recovery.claimed",))
    finally:
        await restarted.close()


async def test_real_host_unknown_completion_can_be_checked_without_a_second_write(
    isolated_workspace,
    monkeypatch,
):
    repo = prepare_repo(isolated_workspace)
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "actual",
                        "name": "write_file",
                        "arguments": {"path": "code.py", "content": "new\n"},
                    }
                ]
            },
            {"text": "verified file state received"},
        ]
    )
    host = RuntimeHost(
        settings_for(repo, state_dir=isolated_workspace / "state"), provider, enable_mcp=False
    )
    original_record = host.file_operations._record

    def fail_completion(record, status, **extra):
        if status == "completed":
            raise OSError("controlled missing completion record")
        return original_record(record, status, **extra)

    monkeypatch.setattr(host.file_operations, "_record", fail_completion)
    try:
        session = host.create_session()
        result = await host.run(RunRequest("update the file", session))
        assert result.status == "interrupted"
        assert host.store.get_run(result.run_id)["reason"] == "unknown_tool_outcome"
        writes_before = len(host.file_operations.records(session))
        check = await host.reconcile_run_outcomes(result.run_id)
        assert check["unresolved"] == 0 and check["checks"][0]["status"] == "verified_after"
        continued = await host.continue_session(session)
        assert continued.status == "completed"
        assert len(host.file_operations.records(session)) == writes_before == 1
        assert (repo / "code.py").read_text() == "new\n"
        assert len(provider.requests) == 2
        pairs = [
            event
            for event in host.store.run_events(continued.run_id)
            if event["type"] == "tool.completed"
        ]
        assert len(pairs) == 1 and pairs[0]["payload"]["call_id"] == "actual"
    finally:
        await host.close()
