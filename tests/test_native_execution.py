"""Native execution profile wiring (ADR-124 / MK-21 slice 2).

Offline only: a fake runner proves the Host/ToolExecutor routing and refusal
semantics; the real stdlib worker runs via the runner's test-only unsandboxed
launcher so the production protocol is exercised without OS wrappers. Real
kernel isolation stays MK-22 and is never claimed here.
"""

import asyncio
import hashlib
import os
import subprocess
from contextlib import suppress
from dataclasses import replace

import pytest

from eventide.executor import ToolCall, ToolContext, ToolExecutor
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.native_execution import (
    NativeExecutionError,
    NativeFileTools,
    build_native_policy,
    cleanup_native_tmp,
    validate_native_profile,
)
from eventide.native_sandbox.policy import NativeBackendKind
from eventide.native_sandbox.probe import BackendDetection
from eventide.native_sandbox.runner import (
    NativeSandboxRunner,
    NativeSandboxUnavailableError,
)
from eventide.policy import PolicyEngine
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for


@pytest.fixture(autouse=True)
def native_probe(monkeypatch):
    """Offline wiring tests must not require a real platform backend binary."""
    def available(kind):
        return BackendDetection(
            kind=kind, platform="test", platform_supported=True,
            binary_path="/test/" + kind.value, available=True,
            detail="Test-only availability; no kernel isolation assertion",
        )

    monkeypatch.setattr("eventide.native_sandbox.probe.detect_backend", available)


class FakeRunner:
    """In-process protocol double recording every dispatched operation."""

    def __init__(self, results: list[dict] | None = None, start_error: Exception | None = None):
        self.ops: list[tuple[str, dict]] = []
        self.results = list(results or [])
        self.start_error = start_error
        self.started = False
        self.closed = False

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def call(self, operation: str, **params: object) -> dict:
        self.ops.append((operation, dict(params)))
        if not self.results:
            raise AssertionError(f"no scripted result for {operation}")
        return self.results.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def real_worker_runner(policy, backend=NativeBackendKind.SEATBELT):
    """Real stdlib worker subprocess without the OS wrapper (offline tests only)."""
    from eventide.native_sandbox.runner import _unsandboxed_test_launcher

    return NativeSandboxRunner(policy, backend=backend, launcher=_unsandboxed_test_launcher)


def make_tools(workspace, state, *, profile="workspace-write", factory=None):
    policy = build_native_policy(workspace, profile=profile, state_root=state)
    tools = NativeFileTools(policy, runner_factory=factory or real_worker_runner)
    return policy, tools


def test_validate_native_profile_fail_closed(monkeypatch):
    valid = {"kind": "native", "profile": "workspace-write", "backend": None}
    assert validate_native_profile(valid)["backend"] in {"seatbelt", "bubblewrap"}
    for broken in (
        {"kind": "sandbox", "profile": "workspace-write", "backend": None},
        {"kind": "native", "profile": "admin", "backend": None},
        {"kind": "native", "profile": "workspace-write", "backend": "docker"},
        {"kind": "native", "profile": "workspace-write"},
        {"kind": "native", "profile": "workspace-write", "backend": None, "extra": 1},
    ):
        with pytest.raises(ValueError):
            validate_native_profile(broken)
    def unavailable(kind):
        return BackendDetection(
            kind=kind, platform="unsupported", platform_supported=False,
            binary_path=None, available=False, detail="probe says missing",
        )

    monkeypatch.setattr("eventide.native_sandbox.probe.detect_backend", unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        validate_native_profile(
            {"kind": "native", "profile": "workspace-write", "backend": "seatbelt"}
        )


def test_policy_protects_state_and_copy(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    state = tmp_path / "state"
    policy = build_native_policy(workspace, profile="read-only", state_root=state)
    assert state in policy.protected_roots
    assert any(str(root).endswith(".eventide") for root in policy.protected_roots)
    assert policy.tmp_root.exists()
    assert not policy.tmp_root.is_relative_to(workspace)
    assert policy.tmp_root not in policy.protected_roots
    assert policy.profile.value == "read-only"
    cleanup_native_tmp(policy)
    assert not policy.tmp_root.exists()


async def test_executor_routes_write_through_worker_not_host(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    (workspace / "code.py").write_text("original\n")
    fake = FakeRunner(results=[{"size": 12}])
    policy, tools = make_tools(
        workspace, tmp_path / "state",
        factory=lambda policy: fake,
    )
    executor = ToolExecutor(tools.handlers)
    result = await executor.execute(
        ToolCall("c1", "write_file", {"path": "code.py", "content": "from worker\n"}),
        ToolContext(workspace, "run_native", PolicyEngine()),
    )
    assert not result.is_error
    assert [op for op, _ in fake.ops] == ["write_file"]
    assert fake.ops[0][1]["overwrite"] is True
    assert (workspace / "code.py").read_text() == "original\n"
    assert tools._runner is fake and fake.started


async def test_unavailable_backend_refuses_without_host_fallback(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    (workspace / "code.py").write_text("original\n")
    factory = lambda policy: FakeRunner(  # noqa: E731
        start_error=NativeSandboxUnavailableError("no sandbox-exec on PATH")
    )
    _, tools = make_tools(workspace, tmp_path / "state", factory=factory)
    output = await tools.read_file("code.py", cwd=workspace)
    assert output.is_error
    assert "refusing to run on the host" in output.content
    assert (workspace / "code.py").read_text() == "original\n"


async def test_readonly_profile_refuses_write_and_scoped_paths_hold(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    (workspace / "code.py").write_text("original\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    _, tools = make_tools(workspace, tmp_path / "state", profile="read-only")
    try:
        denied = await tools.write_file("code.py", "rewritten\n", cwd=workspace)
        assert denied.is_error and "read-only" in denied.content
        assert (workspace / "code.py").read_text() == "original\n"
        outside_write = await tools.write_file(str(outside), "x\n", cwd=workspace)
        assert outside_write.is_error
        assert outside.read_text() == "secret\n"
        os.symlink(outside, workspace / "escape.lnk")
        escaped = await tools.read_file("escape.lnk", cwd=workspace)
        assert escaped.is_error and escaped.metadata.get("code") == "path_denied"
    finally:
        await tools.aclose()
        cleanup_native_tmp(tools.policy)


async def test_worker_read_write_glob_edit_roundtrip(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    (workspace / "code.py").write_text("value = 1\nprint(value)\n")
    (workspace / "note.txt").write_text("hello\n")
    _, tools = make_tools(workspace, tmp_path / "state")
    try:
        read = await tools.read_file("code.py", cwd=workspace)
        assert not read.is_error
        assert "1: value = 1" in read.content and "2: print(value)" in read.content
        digest = hashlib.sha256(b"value = 1\nprint(value)\n").hexdigest()
        mismatch = await tools.edit_file(
            "code.py", "value = 1", "value = 2",
            expected_sha256="0" * 64, cwd=workspace,
        )
        assert mismatch.is_error and "hash mismatch" in mismatch.content
        edited = await tools.edit_file(
            "code.py", "value = 1", "value = 2",
            expected_sha256=digest, cwd=workspace,
        )
        assert not edited.is_error
        assert (workspace / "code.py").read_text() == "value = 2\nprint(value)\n"
        glob = await tools.glob("*.py", cwd=workspace)
        assert glob.content.strip() == "code.py"
        ambiguous = await tools.edit_file("code.py", "value", "x", cwd=workspace)
        assert ambiguous.is_error and "multiple locations" in ambiguous.content
    finally:
        await tools.aclose()
        cleanup_native_tmp(tools.policy)


async def test_close_stops_worker_and_refuses_reuse(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    (workspace / "code.py").write_text("original\n")
    _, tools = make_tools(workspace, tmp_path / "state")
    await tools.read_file("code.py", cwd=workspace)
    tmp_root = tools.policy.tmp_root
    runner = tools._runner
    assert runner is not None and runner._process is not None
    await tools.aclose()
    cleanup_native_tmp(tools.policy)
    assert tools._runner is None
    assert runner._process is None or runner._process.returncode is not None
    assert not tmp_root.exists()
    with pytest.raises(NativeExecutionError, match="closed"):
        await tools.ensure_started()


async def test_cancelled_call_still_shuts_worker_down(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    (workspace / "code.py").write_text("original\n")
    _, tools = make_tools(workspace, tmp_path / "state")
    task = asyncio.ensure_future(tools.read_file("code.py", cwd=workspace))
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await tools.aclose()
    cleanup_native_tmp(tools.policy)
    assert tools._runner is None


@pytest.fixture
def host_worker(monkeypatch):
    monkeypatch.setattr(
        "eventide.native_execution.NativeSandboxRunner",
        lambda policy, backend=None: real_worker_runner(
            policy, backend or NativeBackendKind.SEATBELT,
        ),
    )


def make_host(tmp_path, provider):
    root = make_repo(tmp_path / "source")
    (root / "code.py").write_text("original\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "code.py"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "code"],
        check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@e.invalid",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@e.invalid"},
    )
    return RuntimeHost(
        replace(settings_for(root), state_dir=tmp_path / "state"), provider
    )


async def test_native_host_run_routes_file_tools_through_worker(tmp_path, host_worker):
    provider = ScriptedProvider([
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "code.py"}}]},
        {"text": "done"},
    ])
    host = make_host(tmp_path, provider)
    try:
        result = await host.run(RunRequest(
            "inspect", execution_target="worktree", execution_profile="native",
            native_profile="workspace-write",
        ))
        assert result.status == "completed"
        intent = host._admitted_intent(result.run_id)
        assert intent["execution_profile_config"]["kind"] == "native"
        assert intent["execution_profile_config"]["profile"] == "workspace-write"
        assert intent["execution_binding"]["kind"] == "worktree"
        names = {tool["name"] for tool in provider.requests[0].tools}
        assert "bash" not in names and "search_text" not in names
        assert {"read_file", "write_file", "edit_file", "glob"} <= names
        completed = [
            event for event in host.store.run_events(result.run_id)
            if event["type"] == "tool.completed"
            and event["payload"].get("name") == "read_file"
        ]
        assert completed, "read_file must complete inside the run"
        evidence = completed[0]["payload"].get("metadata", {}).get("source_evidence", {})
        assert evidence.get("native_worker") is True
        assert "1: original" in str(completed[0]["payload"].get("content", ""))
        state = host.session_status(result.session_id)
        assert state["execution_profile"] == "native"
        assert state["native_profile"]["kind"] == "native"
        assert (host.settings.workdir / "code.py").read_text() == "original\n"
        catalog = [
            event for event in host.store.run_events(result.run_id)
            if event["type"] == "tool.catalog.frozen"
        ]
        assert catalog and catalog[0]["payload"]["environment"] == "native"
    finally:
        await host.close()


async def test_native_admission_rejects_source_target_and_switch(tmp_path, host_worker):
    provider = ScriptedProvider([{"text": "unused"}])
    host = make_host(tmp_path, provider)
    try:
        with pytest.raises(ValueError, match="worktree"):
            await host.run(RunRequest("inspect", execution_profile="native"))
        first = await host.run(RunRequest(
            "inspect", execution_target="worktree", execution_profile="native",
        ))
        with pytest.raises(ValueError, match="fixed for this Session"):
            await host.run(RunRequest("second try", session_id=first.session_id))
        with pytest.raises(ValueError, match="Skills are disabled"):
            await host.run(RunRequest(
                "inspect", execution_target="worktree", execution_profile="native",
                selected_skills=["demo"],
            ))
        with pytest.raises(ValueError, match="Sandbox image"):
            await host.run(RunRequest(
                "inspect", execution_target="worktree", execution_profile="native",
                sandbox_image="sha256:" + "a" * 64,
            ))
    finally:
        await host.close()


async def test_read_pages_match_local_contract_and_bind_copy_version(tmp_path):
    from eventide.tools.filesystem import read_file_output

    workspace = tmp_path / "copy"
    workspace.mkdir()
    target = workspace / "long.py"
    target.write_bytes(("甲" * 15_000 + "\r\nsecond\r\n").encode())
    _, tools = make_tools(workspace, tmp_path / "state")
    try:
        first = await tools.read_file("long.py", cwd=workspace)
        expected = read_file_output("long.py", cwd=workspace)
        assert first.content == expected.content
        evidence = first.metadata["source_evidence"]
        assert evidence["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
        token = evidence["next_token"]
        assert token and evidence["end_character"] > 0
        second = await tools.read_file("long.py", continuation=token, cwd=workspace)
        assert second.content == read_file_output(
            "long.py", continuation=token, cwd=workspace,
        ).content
        target.write_bytes(b"")
        stale = await tools.read_file("long.py", continuation=token, cwd=workspace)
        assert stale.is_error and stale.metadata["code"] == "stale_page"
    finally:
        await tools.aclose()
        cleanup_native_tmp(tools.policy)


async def test_worker_creates_files_and_refuses_ambiguous_or_changed_edits(tmp_path):
    workspace = tmp_path / "copy"
    workspace.mkdir()
    _, tools = make_tools(workspace, tmp_path / "state")
    try:
        made = await tools.write_file("nested/new.txt", "aaa", cwd=workspace)
        assert not made.is_error
        target = workspace / "nested/new.txt"
        target.chmod(0o750)
        assert not (await tools.write_file("nested/new.txt", "aaa", cwd=workspace)).is_error
        assert target.stat().st_mode & 0o777 == 0o750
        for old, code in (("aa", "ambiguous"), ("", "invalid_request")):
            result = await tools.edit_file("nested/new.txt", old, "x", cwd=workspace)
            assert result.is_error and result.metadata["code"] == code
            assert target.read_text() == "aaa"
        digest = hashlib.sha256(b"aaa").hexdigest()
        target.write_text("changed")
        changed = await tools.edit_file(
            "nested/new.txt", "changed", "x", expected_sha256=digest, cwd=workspace,
        )
        assert changed.is_error and changed.metadata["code"] == "hash_mismatch"
        assert target.read_text() == "changed"
        os.link(target, workspace / "hardlink.txt")
        assert (await tools.read_file("hardlink.txt", cwd=workspace)).is_error
        denied = await tools.edit_file("hardlink.txt", "changed", "x", cwd=workspace)
        assert denied.is_error and denied.metadata["code"] == "path_denied"
    finally:
        await tools.aclose()
        cleanup_native_tmp(tools.policy)


async def test_state_ancestor_grants_only_copy_and_protects_shared_git(tmp_path):
    state = tmp_path / "state"
    workspace = state / "worktrees" / "candidate"
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_text("allowed")
    (state / "secret.key").write_text("secret")
    git = tmp_path / "source" / ".git"
    git.mkdir(parents=True)
    policy = build_native_policy(
        workspace, profile="workspace-write", state_root=state, git_common_dir=git,
    )
    tools = NativeFileTools(policy, runner_factory=real_worker_runner)
    try:
        assert not (await tools.read_file("a.txt", cwd=workspace)).is_error
        assert (await tools.read_file(str(state / "secret.key"), cwd=workspace)).is_error
        assert git in policy.protected_roots
    finally:
        await tools.aclose()
        cleanup_native_tmp(policy)


async def test_write_with_lost_worker_result_is_unknown(tmp_path):
    from eventide.native_sandbox.runner import WorkerCrashedError

    class Crashed(FakeRunner):
        async def call(self, operation, **params):
            raise WorkerCrashedError("response lost after dispatch")

    workspace = tmp_path / "copy"
    workspace.mkdir()
    _, tools = make_tools(workspace, tmp_path / "state", factory=lambda _: Crashed())
    try:
        result = await tools.write_file("a.txt", "x", cwd=workspace)
        assert result.is_error and result.metadata["outcome_unknown"] is True
    finally:
        await tools.aclose()
        cleanup_native_tmp(tools.policy)


async def test_cancel_during_start_waits_for_cleanup_and_reports_cleanup_errors(tmp_path):
    started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Waiting(FakeRunner):
        async def start(self):
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await release.wait()
            self.closed = True

    workspace = tmp_path / "copy"
    workspace.mkdir()
    fake = Waiting()
    _, tools = make_tools(workspace, tmp_path / "state", factory=lambda _: fake)
    task = asyncio.create_task(tools.ensure_started())
    await started.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed
    cleanup_native_tmp(tools.policy)

    class BrokenClose(FakeRunner):
        async def aclose(self):
            raise OSError("cannot close")

    _, other = make_tools(workspace, tmp_path / "state", factory=lambda _: BrokenClose())
    await other.ensure_started()
    with pytest.raises(OSError, match="cannot close"):
        await other.aclose()
    cleanup_native_tmp(other.policy)


async def test_native_readonly_catalog_and_session_permission_freeze(tmp_path, host_worker):
    provider = ScriptedProvider([{"text": "first"}, {"text": "second"}])
    host = make_host(tmp_path, provider)
    try:
        first = await host.run(RunRequest(
            "inspect", execution_target="worktree", execution_profile="native",
            native_profile="read-only",
        ))
        assert first.status == "completed"
        from eventide.agent_graph import GraphDefinition, projections

        with pytest.raises(ValueError, match="unavailable for native"):
            host.agent_graphs.create(first.run_id, GraphDefinition.model_validate({
                "title": "Must stay isolated", "nodes": [{"id": "read", "goal": "inspect"}],
            }), "native-graph")
        assert projections(host.store) == []
        assert len(provider.requests) == 1
        with pytest.raises(ValueError, match="unavailable for native"):
            await host.create_side_question(first.run_id, "explain")
        from eventide.side_questions import create_side_question

        with pytest.raises(ValueError, match="unavailable for native"):
            await create_side_question(host, first.run_id, "direct public call")
        names = {tool["name"] for tool in provider.requests[0].tools}
        assert not names & {"bash", "write_file", "edit_file", "delegate_readonly", "load_skill"}
        second = await host.run(RunRequest(
            "next", session_id=first.session_id, execution_target="worktree",
            execution_profile="native",
        ))
        assert second.status == "completed"
        frozen = host._admitted_intent(second.run_id)["execution_profile_config"]
        assert frozen["profile"] == "read-only"
        with pytest.raises(ValueError, match="fixed"):
            await host.run(RunRequest(
                "escalate", session_id=first.session_id, execution_target="worktree",
                execution_profile="native", native_profile="workspace-write",
            ))
        with pytest.raises(ValueError, match="requires native"):
            await host.run(RunRequest("wrong", native_profile="read-only"))
    finally:
        await host.close()


async def test_host_native_write_stays_in_copy_and_skips_host_integrations(
    tmp_path, host_worker, monkeypatch,
):
    provider = ScriptedProvider([
        {"tool_calls": [{"name": "write_file", "arguments": {
            "path": "nested/new.txt", "content": "candidate",
        }}]},
        {"text": "done"},
    ])
    host = make_host(tmp_path, provider)

    def forbidden(*args, **kwargs):
        raise AssertionError("Native tasks must not prepare host Skills/MCP")

    monkeypatch.setattr(host.skills, "catalog", forbidden)
    monkeypatch.setattr("eventide.host.MCPManager.load_configs", forbidden)
    try:
        result = await host.run(RunRequest(
            "write candidate", execution_profile="native", execution_target="worktree",
        ))
        assert result.status == "completed"
        binding = host._execution_binding(result.run_id)
        assert binding is not None
        root, _ = host._execution_paths(binding)
        assert (root / "nested/new.txt").read_text() == "candidate"
        assert not (host.settings.workdir / "nested").exists()
        assert host.session_status(result.session_id)["sandbox_profile"] is None
    finally:
        await host.close()


def test_native_capabilities_api_separates_probe_from_verification(tmp_path):
    from fastapi.testclient import TestClient

    from eventide.api import create_app

    host = make_host(tmp_path, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        result = client.get("/api/runtime/native-capabilities")
        assert result.status_code == 200
        body = result.json()
        assert body["verification"] == "unverified"
        assert {item["kind"] for item in body["backends"]} == {"seatbelt", "bubblewrap"}
        session = host.create_session()
        invalid = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "invalid", "native_profile": "read-only"},
            headers={"Origin": "http://testserver", "Idempotency-Key": "native-invalid"},
        )
        assert invalid.status_code == 422
        assert not host.store.latest_run(session)


@pytest.mark.parametrize("operation", ["write_file", "edit_file"])
async def test_cancel_after_native_write_dispatch_persists_unknown_and_waits_for_worker(
    tmp_path, monkeypatch, operation,
):
    import json

    from eventide.native_sandbox.runner import _unsandboxed_test_launcher

    written, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runners = []

    class HeldResponse(NativeSandboxRunner):
        async def start(self):
            await super().start()
            reader = self._process.stdout
            original = reader.readline

            async def hold_result():
                raw = await original()
                response = json.loads(raw)
                if response.get("ok") and set(response.get("result", {})) == {"size"}:
                    written.set()
                    await asyncio.Event().wait()
                return raw

            reader.readline = hold_result

        async def aclose(self):
            closing.set()
            await release_close.wait()
            await super().aclose()

    def factory(policy, backend=None):
        runner = HeldResponse(
            policy, backend=NativeBackendKind.SEATBELT,
            launcher=_unsandboxed_test_launcher,
        )
        runners.append(runner)
        return runner

    monkeypatch.setattr("eventide.native_execution.NativeSandboxRunner", factory)
    args = {"path": "code.py", "content": "changed\n"} if operation == "write_file" else {
        "path": "code.py", "old_text": "original", "new_text": "changed",
    }
    provider = ScriptedProvider([
        {"tool_calls": [{"name": operation, "arguments": args}]},
        {"text": "must not continue"},
    ])
    host = make_host(tmp_path, provider)
    run_id = "run_native_cancelled_write"
    task = asyncio.create_task(host.run(RunRequest(
        "change", run_id=run_id, execution_target="worktree", execution_profile="native",
    )))
    try:
        await asyncio.wait_for(written.wait(), 5)
        binding = host._execution_binding(run_id)
        root, _ = host._execution_paths(binding)
        assert (root / "code.py").read_text() == "changed\n"
        owner = host._run_tasks[run_id]
        owner.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        process = runners[0]._process
        assert process is not None and process.returncode is None
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode is not None
        record = host.store.get_run(run_id)
        assert record["reason"] == "unknown_tool_outcome"
        unknown = [event for event in host.store.run_events(run_id)
                   if event["type"] == "tool.outcome_unknown"]
        assert len(unknown) == 1 and unknown[0]["payload"]["name"] == operation
        assert len(provider.requests) == 1
        with pytest.raises(ValueError, match="Unknown tool outcome"):
            host.validate_continue(record["session_id"])
        settings = host.settings
        session_id = record["session_id"]
    finally:
        release_close.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await host.close()
    next_provider = ScriptedProvider([{"text": "must not recover"}])
    reopened = RuntimeHost(settings, next_provider)
    try:
        await reopened.initialize()
        assert reopened.store.latest_run(session_id)["id"] == run_id
        assert not next_provider.requests
    finally:
        await reopened.close()


async def test_cancel_before_native_write_dispatch_does_not_claim_unknown(tmp_path, monkeypatch):
    starting = asyncio.Event()
    runners = []

    class BeforeDispatch(FakeRunner):
        async def start(self):
            starting.set()
            await asyncio.Event().wait()

    def factory(policy, backend=None):
        runner = BeforeDispatch()
        runners.append(runner)
        return runner

    monkeypatch.setattr("eventide.native_execution.NativeSandboxRunner", factory)
    provider = ScriptedProvider([{"tool_calls": [{"name": "write_file", "arguments": {
        "path": "code.py", "content": "not dispatched",
    }}]}])
    host = make_host(tmp_path, provider)
    run_id = "run_native_before_dispatch"
    task = asyncio.create_task(host.run(RunRequest(
        "write", run_id=run_id, execution_target="worktree", execution_profile="native",
    )))
    try:
        await asyncio.wait_for(starting.wait(), 5)
        host._run_tasks[run_id].cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = host.store.run_events(run_id)
        assert not any(event["type"] == "tool.outcome_unknown" for event in events)
        assert runners[0].closed and not runners[0].ops
        root, _ = host._execution_paths(host._execution_binding(run_id))
        assert (root / "code.py").read_text() == "original\n"
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await host.close()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_edit_checks_directory_entry_before_and_after_writing(tmp_path, monkeypatch, phase):
    import io

    from eventide.native_sandbox.worker import FileWorker

    workspace = tmp_path / "copy"
    workspace.mkdir()
    target = workspace / "code.txt"
    target.write_text("original")
    replacement = workspace / "replacement.txt"
    replacement.write_text("replacement")
    policy = build_native_policy(
        workspace, profile="workspace-write", state_root=tmp_path / "state",
    )
    worker = FileWorker(policy.grant(), io.BytesIO(), io.BytesIO())
    held = os.open(target, os.O_RDONLY)
    original_stat = os.stat
    calls = 0

    def replace_on_check(path, *args, **kwargs):
        nonlocal calls
        if path == "code.txt" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == (1 if phase == "before" else 2):
                os.replace(replacement, target)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr("eventide.native_sandbox.worker.os.stat", replace_on_check)
    try:
        result = worker.handle({"v": 1, "id": "edit", "op": "edit_file", "params": {
            "path": str(target), "old_text": "original", "new_text": "changed",
        }})
        assert not result["ok"]
        assert result["error"]["code"] == ("hash_mismatch" if phase == "before" else "path_changed")
        assert target.read_text() == "replacement"
        assert os.read(held, 100) == (b"original" if phase == "before" else b"changed")
    finally:
        os.close(held)
        cleanup_native_tmp(policy)


async def test_cancelled_dispatched_write_keeps_unknown_when_cleanup_fails(tmp_path):
    from eventide.native_sandbox.runner import WorkerCallCancelledError

    class CleanupFailed(FakeRunner):
        async def call(self, operation, **params):
            raise WorkerCallCancelledError(operation, dispatched=True)

        async def aclose(self):
            raise OSError("worker cleanup blocked")

    workspace = tmp_path / "copy"
    workspace.mkdir()
    _, tools = make_tools(workspace, tmp_path / "state", factory=lambda _: CleanupFailed())
    try:
        result = await tools.write_file("code.py", "x", cwd=workspace)
        assert result.is_error and result.metadata["outcome_unknown"] is True
        assert "cleanup blocked" in result.content
        assert tools._runner is not None
    finally:
        cleanup_native_tmp(tools.policy)


@pytest.mark.parametrize("change", ["replace", "inplace"])
def test_write_receipt_checks_published_inode_and_content(tmp_path, monkeypatch, change):
    import base64
    import io

    from eventide.native_sandbox.worker import FileWorker

    workspace = tmp_path / "copy"
    workspace.mkdir()
    target = workspace / "code.txt"
    replacement = workspace / "editor.txt"
    replacement.write_text("editor")
    policy = build_native_policy(
        workspace, profile="workspace-write", state_root=tmp_path / "state",
    )
    worker = FileWorker(policy.grant(), io.BytesIO(), io.BytesIO())
    original_replace = os.replace
    published = []

    def interfere(source, destination, *args, **kwargs):
        original_replace(source, destination, *args, **kwargs)
        if destination == "code.txt" and kwargs.get("dst_dir_fd") is not None:
            published.append((target.stat().st_ino, target.read_text()))
            if change == "replace":
                original_replace(replacement, target)
            else:
                target.write_text("editor")

    monkeypatch.setattr("eventide.native_sandbox.worker.os.replace", interfere)
    try:
        result = worker.handle({"v": 1, "id": "write", "op": "write_file", "params": {
            "path": str(target), "overwrite": True,
            "content_b64": base64.b64encode(b"agent").decode(),
        }})
        assert published and published[0][1] == "agent"
        assert not result["ok"] and result["error"]["code"] == "path_changed"
        assert "published" in result["error"]["message"]
        assert target.read_text() == "editor"
        assert (target.stat().st_ino == published[0][0]) == (change == "inplace")
    finally:
        cleanup_native_tmp(policy)


@pytest.mark.parametrize("change", ["replace", "inplace"])
async def test_published_write_change_projects_unknown_in_host(tmp_path, monkeypatch, change):
    import io

    from eventide.native_sandbox.runner import WorkerOperationError
    from eventide.native_sandbox.worker import FileWorker

    workers = []

    class InProcessWorker(FakeRunner):
        def __init__(self, policy):
            super().__init__()
            self.worker = FileWorker(policy.grant(), io.BytesIO(), io.BytesIO())

        async def call(self, operation, **params):
            result = self.worker.handle({"v": 1, "id": "test", "op": operation, "params": params})
            if not result["ok"]:
                raise WorkerOperationError(result["error"]["code"], result["error"]["message"])
            return result["result"]

    def factory(policy, backend=None):
        worker = InProcessWorker(policy)
        workers.append(worker)
        return worker

    original_replace = os.replace

    def interfere(source, destination, *args, **kwargs):
        original_replace(source, destination, *args, **kwargs)
        if destination == "code.py" and kwargs.get("dst_dir_fd") is not None:
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW,
                dir_fd=kwargs["dst_dir_fd"],
            ) if change == "inplace" else os.open(
                "editor.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600, dir_fd=kwargs["dst_dir_fd"],
            )
            with os.fdopen(descriptor, "w") as editor:
                editor.write("editor\n")
            if change == "replace":
                original_replace("editor.tmp", destination, **kwargs)

    monkeypatch.setattr("eventide.native_execution.NativeSandboxRunner", factory)
    monkeypatch.setattr("eventide.native_sandbox.worker.os.replace", interfere)
    provider = ScriptedProvider([
        {"tool_calls": [{"name": "write_file", "arguments": {
            "path": "code.py", "content": "agent\n",
        }}]},
        {"text": "must not continue"},
    ])
    host = make_host(tmp_path, provider)
    try:
        result = await host.run(RunRequest(
            "write", execution_profile="native", execution_target="worktree",
        ))
        assert result.status == "interrupted"
        assert host.store.get_run(result.run_id)["reason"] == "unknown_tool_outcome"
        unknown = [event for event in host.store.run_events(result.run_id)
                   if event["type"] == "tool.outcome_unknown"]
        assert len(unknown) == 1 and "published" in unknown[0]["payload"]["error"]
        root, _ = host._execution_paths(host._execution_binding(result.run_id))
        assert (root / "code.py").read_text() == "editor\n"
        assert len(provider.requests) == 1 and workers[0].closed
    finally:
        await host.close()
