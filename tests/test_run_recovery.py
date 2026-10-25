"""MK-08C persistent auto-recovery: real Host restart wiring, claims and budgets."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from eventide.budgets import BudgetExceeded, BudgetScope
from eventide.host import RuntimeHost
from eventide.model_budget import ModelBudgetController
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from eventide.workspace import workspace_checkpoint
from tests.test_runtime import settings_for


def make_repo(path: Path) -> Path:
    path.mkdir()
    for args in (
        ["init"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
        ["commit", "--allow-empty", "-m", "initial"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


async def wait_terminal(host: RuntimeHost, run_id: str, timeout: float = 5.0) -> str:
    async def poll() -> str:
        while True:
            if host.store.run_is_terminal(run_id):
                events = host.store.run_events(run_id)
                return next(
                    e["type"] for e in reversed(events)
                    if e["type"].startswith("run.")
                )
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout)


def crash_host(host: RuntimeHost) -> None:
    """Release the Host lease without finishing its Runs, like a killed process."""
    host._lease.close()
    host.store.close()


def continuation_runs(store: RuntimeStore, old_run: str) -> list[str]:
    rows = store._connection.execute(
        "SELECT r.id FROM runs r JOIN turns t ON r.turn_id=t.id WHERE t.continuation_of=?",
        (old_run,),
    ).fetchall()
    return [row["id"] for row in rows]


async def test_restart_auto_resumes_queued_run(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        intent = host._admitted_intent(old_run)
        assert intent["recovery_policy"] == {"auto_resume": True, "version": 1}
    finally:
        crash_host(host)

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "resumed output"}])
    )
    try:
        await restarted.initialize()
        old_events = restarted.store.run_events(old_run)
        claims = [e for e in old_events if e["type"] == "recovery.claimed"]
        assert len(claims) == 1
        successor = claims[0]["payload"]["resumed_as"]
        assert any(e["type"] == "run.interrupted" for e in old_events)
        identity = restarted.store.get_run_identity(successor)
        assert identity is not None and identity["continuation_of"] == old_run
        assert (await wait_terminal(restarted, successor)) == "run.completed"
    finally:
        await restarted.close()

    # A consecutive restart must not mint a second successor.
    third = RuntimeHost(settings_for(isolated_workspace))
    try:
        assert continuation_runs(third.store, old_run) == [successor]
        assert third.store.claim_run_recovery(
            session, old_run, "run_should_not_exist",
            finalize_payload={}, claim_payload={},
        ) is False
    finally:
        await third.close()


async def test_unknown_tool_outcome_stays_parked(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.store.append_event(old_run, "tool.outcome_unknown", {
            "call_id": "call_1", "name": "bash", "readonly": False,
            "workspace_id": workspace.workspace_id,
            "error": "Host stopped before the tool reported",
        })
    finally:
        crash_host(host)

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        events = restarted.store.run_events(old_run)
        interrupted = [e for e in events if e["type"] == "run.interrupted"]
        assert len(interrupted) == 1
        assert interrupted[0]["payload"]["reason"] == "unknown_tool_outcome"
        assert not [e for e in events if e["type"] == "recovery.claimed"]
        assert continuation_runs(restarted.store, old_run) == []
    finally:
        await restarted.close()


async def test_restart_without_frozen_policy_never_upgrades_history(isolated_workspace):
    state = isolated_workspace / "legacy"
    state.mkdir()
    repo = make_repo(state / "repo")
    legacy = RuntimeStore(state / ".eventide" / "runtime.sqlite")
    legacy_workspace = legacy.register_workspace(repo, None)
    legacy.create_session("legacy_session", legacy_workspace.workspace_id)
    legacy_run = "run_legacy000001"
    legacy.admit_run(legacy_run, "legacy_session", [
        ("run.admitted", {
            "mode": "auto", "model": "test-model", "provider": "scripted",
            "session_id": "legacy_session", "workspace_id": legacy_workspace.workspace_id,
            "attachment_ids": [], "continuation_of": None,
        }, "system", "user"),
        ("message.user", {"message": {"role": "user", "content": [
            {"type": "text", "text": "finish the task"},
        ]}}, "user", "user"),
        (
            "workspace.checkpoint",
            {"checkpoint": workspace_checkpoint(Path(str(legacy_workspace.path)))},
            "system", "runtime",
        ),
    ])
    legacy.close()

    restarted = RuntimeHost(
        settings_for(state), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        events = restarted.store.run_events(legacy_run)
        interrupted = [e for e in events if e["type"] == "run.interrupted"]
        assert len(interrupted) == 1
        assert interrupted[0]["payload"]["reason"] == "no_frozen_recovery_policy"
        assert not [e for e in events if e["type"] == "recovery.claimed"]
        assert continuation_runs(restarted.store, legacy_run) == []
    finally:
        await restarted.close()


def test_recovery_continuation_shares_root_run_budget(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "budget.sqlite")
    workspace_record = store.register_workspace(isolated_workspace, None)
    store.create_session("s", workspace_record.workspace_id)
    store.create_run("root", "s")
    store.append_event("root", "run.admitted", {
        "model": "m", "recovery_policy": {"auto_resume": True, "version": 1},
    })
    store.create_run("resumed", "s", continuation_of="root")
    store.append_event("resumed", "run.admitted", {"model": "m", "recovery_of": "root"})
    controller = ModelBudgetController(store)
    session_id, context = controller.context("resumed")
    assert session_id == "s"
    assert context.root_run_id == "root"
    ledger = controller.ledger
    ledger.configure(
        "s", scope=BudgetScope("run", "root"), token_limit=100, request_id="cfg",
    )
    ledger.reserve(
        "s", "resumed", "a1", context=context,
        input_estimate=60, output_estimate=30, estimate_method="test tokenizer v1",
    )
    with pytest.raises(BudgetExceeded):
        ledger.reserve(
            "s", "resumed", "a2", context=context,
            input_estimate=10, output_estimate=10, estimate_method="test tokenizer v1",
        )
    store.close()


async def test_reclaim_after_crash_between_claim_and_initialization(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    first = RuntimeHost(settings_for(isolated_workspace))
    workspace = first.resolve_or_register_workspace(repo)
    session = first.create_session(workspace_id=workspace.workspace_id)
    old_run = await first.admit_run(RunRequest("finish the task", session))
    crash_host(first)
    second = RuntimeHost(settings_for(isolated_workspace))
    claims = [e for e in second.store.run_events(old_run) if e["type"] == "recovery.claimed"]
    successor = claims[0]["payload"]["resumed_as"]
    crash_host(second)
    third = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    try:
        await third.initialize()
        assert third.store.get_run_identity(successor) is not None
        assert await wait_terminal(third, successor) == "run.completed"
        assert continuation_runs(third.store, old_run) == [successor]
    finally:
        await third.close()


async def test_recovery_preserves_explicit_model_and_worktree(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    first = RuntimeHost(settings_for(isolated_workspace))
    workspace = first.resolve_or_register_workspace(repo)
    session = first.create_session(workspace_id=workspace.workspace_id)
    old_run = await first.admit_run(RunRequest(
        "finish the task", session, model="selected-model", execution_target="worktree",
    ))
    binding = first._execution_binding(old_run)
    crash_host(first)
    second = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    try:
        await second.initialize()
        successors = continuation_runs(second.store, old_run)
        assert len(successors) == 1
        successor = successors[0]
        assert second._admitted_intent(successor)["model"] == "selected-model"
        assert second._execution_binding(successor) == binding
        assert await wait_terminal(second, successor) == "run.completed"
        assert ModelBudgetController(second.store).context(successor)[1].root_run_id == old_run
    finally:
        await second.close()


def test_api_recovered_run_can_receive_new_approval(isolated_workspace):
    import time
    import uuid

    from fastapi.testclient import TestClient

    from eventide.api import create_app

    repo = make_repo(isolated_workspace / "repo")
    first = RuntimeHost(settings_for(isolated_workspace))
    workspace = first.resolve_or_register_workspace(repo)
    session = first.create_session(workspace_id=workspace.workspace_id)
    old_run = asyncio.run(first.admit_run(RunRequest("finish task", session)))
    crash_host(first)
    restarted = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"tool_calls": [{"id": "ask", "name": "bash", "arguments": {"command": "rm scratch.txt"}}]},
        {"text": "done"},
    ]))
    with TestClient(create_app(restarted)) as client:
        deadline = time.monotonic() + 3
        while not client.app.state.approvals.pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.app.state.approvals.pending
        successor = continuation_runs(restarted.store, old_run)[0]
        approval_id = next(iter(client.app.state.approvals.pending))
        response = client.post(
            f"/api/runs/{successor}/approvals/{approval_id}", json={"approved": False},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert response.status_code == 200
