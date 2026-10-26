"""MK-08C graceful service shutdown: recoverable terminals vs explicit user stops.

Real Host close + real Host restart wiring. The durable facts decide: a
graceful close persists one Run-bound ``host.shutdown_started`` marker per
unfinished Run before cancelling anything and leaves
``run.interrupted``/``reason=service_shutdown`` terminals, while a user stop
persists ``run.cancel_requested`` and always beats shutdown, whatever order
they arrive in. Markers name the exact Run and shutdown, so a later normal
cancel in the same session can never inherit an older shutdown's identity.
"""

import asyncio
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from eventide.host import RuntimeHost
from eventide.model_budget import ModelBudgetController
from eventide.models import ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
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


def reopen_store(host: RuntimeHost) -> RuntimeStore:
    """A fresh read handle after close() released the Host's own connection."""
    return RuntimeStore(host.store.path)


def continuation_runs(store: RuntimeStore, old_run: str) -> list[str]:
    rows = store._connection.execute(
        "SELECT r.id FROM runs r JOIN turns t ON r.turn_id=t.id WHERE t.continuation_of=?",
        (old_run,),
    ).fetchall()
    return [row["id"] for row in rows]


def shutdown_terminal(store: RuntimeStore, run_id: str) -> dict[str, Any] | None:
    """The Run's newest terminal fact, but only when it is shutdown-resumable."""
    for event in reversed(store.run_events(run_id)):
        if event["type"] in {"run.completed", "run.failed", "run.interrupted"}:
            if (event["type"] == "run.interrupted"
                    and event["payload"].get("reason") == "service_shutdown"):
                return event
            return None
    return None


def claimed(store: RuntimeStore, session_id: str) -> list[dict[str, Any]]:
    return [
        e for e in store.session_events(session_id) if e["type"] == "recovery.claimed"
    ]


async def wait_terminal(store: RuntimeStore, run_id: str, timeout: float = 5.0) -> str:
    async def poll() -> str:
        while True:
            events = store.run_events(run_id)
            terminals = [
                e["type"] for e in events
                if e["type"] in {"run.completed", "run.failed", "run.interrupted"}
            ]
            if terminals:
                return terminals[-1]
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout)


async def wait_model_started(provider: Any, timeout: float = 5.0) -> None:
    await asyncio.wait_for(provider.started.wait(), timeout)


class BlockedModelProvider:
    """Provider that holds the model step open, like a slow remote call."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(self, request: Any) -> Any:
        self.started.set()
        await asyncio.Event().wait()  # only close() can end this


class ScriptedThenBlockingProvider:
    """Serves scripted responses first; further model calls block until close()."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.started = asyncio.Event()

    async def complete(self, request: Any) -> Any:
        self.started.set()
        if self.responses:
            return ModelResponse(str(self.responses.pop(0).get("text", "")))
        await asyncio.Event().wait()  # only close() can end this


async def test_close_marks_active_run_recoverable_and_next_host_resumes(
    isolated_workspace,
):
    repo = make_repo(isolated_workspace / "repo")
    provider = BlockedModelProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.start_admitted_run(old_run)
        await wait_model_started(provider)
    finally:
        await host.close()

    facts = reopen_store(host)
    try:
        terminal = shutdown_terminal(facts, old_run)
        assert terminal is not None
        assert terminal["payload"]["reason"] == "service_shutdown"
        assert terminal["payload"]["resumable"] is True
        assert terminal["payload"]["shutdown_id"]
        assert facts.service_shutdown_runs() == [old_run]
        markers = [
            e for e in facts.session_events(session)
            if e["type"] == "host.shutdown_started"
        ]
        # The marker is bound to this exact Run and this shutdown.
        assert len(markers) == 1
        assert markers[0]["payload"]["shutdown_id"] == terminal["payload"]["shutdown_id"]
        assert markers[0]["payload"]["run_id"] == old_run
        assert markers[0]["run_id"] == old_run
    finally:
        facts.close()

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "resumed output"}])
    )
    try:
        await restarted.initialize()
        claims = claimed(restarted.store, session)
        assert len(claims) == 1
        # The shutdown claim is a session fact bound precisely to both identities.
        assert claims[0]["run_id"] is None
        assert claims[0]["payload"]["source_run_id"] == old_run
        assert claims[0]["payload"]["origin"] == "service_shutdown"
        successor = claims[0]["payload"]["resumed_as"]
        assert successor != old_run
        # The original terminal was not modified and not duplicated.
        interrupted = [
            e for e in restarted.store.run_events(old_run) if e["type"] == "run.interrupted"
        ]
        assert len(interrupted) == 1
        assert continuation_runs(restarted.store, old_run) == [successor]
        assert await wait_terminal(restarted.store, successor) == "run.completed"
        context_session, context = ModelBudgetController(
            restarted.store
        ).context(successor)
        assert context_session == session
        assert context.root_run_id == old_run
    finally:
        await restarted.close()


async def test_close_keeps_queued_worktree_run_recoverable(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest(
            "finish the task", session, execution_target="worktree",
        ))
        binding = host._execution_binding(old_run)
        assert binding["kind"] == "worktree"
    finally:
        await host.close()

    facts = reopen_store(host)
    terminal = shutdown_terminal(facts, old_run)
    facts.close()
    assert terminal is not None and terminal["payload"]["resumable"] is True

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}])
    )
    try:
        await restarted.initialize()
        successors = continuation_runs(restarted.store, old_run)
        assert len(successors) == 1
        assert restarted._execution_binding(successors[0]) == binding
        assert await wait_terminal(restarted.store, successors[0]) == "run.completed"
    finally:
        await restarted.close()


async def test_user_stop_then_close_never_resumes(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        record = await host.cancel_run(old_run)
        assert record["status"] == "interrupted"
    finally:
        await host.close()

    facts = reopen_store(host)
    terminal = shutdown_terminal(facts, old_run)
    facts.close()
    assert terminal is None  # plain cancelled terminal, never a shutdown one
    cancelled = reopen_store(host)
    try:
        plain = next(
            e for e in cancelled.run_events(old_run) if e["type"] == "run.interrupted"
        )
        # The explicit stop keeps its original reason; close() did not rewrite it.
        assert plain["payload"]["reason"] == "cancelled"
        assert cancelled.service_shutdown_runs() == []
    finally:
        cancelled.close()

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        assert continuation_runs(restarted.store, old_run) == []
        assert not claimed(restarted.store, session)
    finally:
        await restarted.close()


async def test_user_stop_racing_close_stays_parked(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    provider = BlockedModelProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.start_admitted_run(old_run)
        await wait_model_started(provider)
        # Whatever order wins, the durable user stop must keep the Run parked.
        await asyncio.gather(host.close(), host.cancel_run(old_run))
    finally:
        await host.close()

    facts = reopen_store(host)
    try:
        assert any(
            e["type"] == "run.cancel_requested"
            and e["payload"].get("run_id") == old_run
            for e in facts.session_events(session)
        ), "user stop must be durable even when racing shutdown"
    finally:
        facts.close()

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        assert continuation_runs(restarted.store, old_run) == []
        assert not claimed(restarted.store, session)
    finally:
        await restarted.close()


async def test_unknown_outcome_stays_parked_after_shutdown(isolated_workspace):
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
        await host.close()

    facts = reopen_store(host)
    terminal = shutdown_terminal(facts, old_run)
    facts.close()
    assert terminal is not None and terminal["payload"]["resumable"] is True

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        assert continuation_runs(restarted.store, old_run) == []
        assert not claimed(restarted.store, session)
        # The original shutdown terminal is the only terminal fact.
        assert len([
            e for e in restarted.store.run_events(old_run) if e["type"] == "run.interrupted"
        ]) == 1
    finally:
        await restarted.close()


async def test_pending_approval_stays_parked_after_shutdown(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.store.append_event(old_run, "approval.required", {
            "approval_id": "ask_1", "name": "bash", "arguments": {"command": "ls"},
        })
    finally:
        await host.close()

    facts = reopen_store(host)
    facts.close()
    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        assert continuation_runs(restarted.store, old_run) == []
        assert not claimed(restarted.store, session)
    finally:
        await restarted.close()


async def test_existing_terminals_are_not_resumed_or_touched(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"text": "first output"},
    ]))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        completed = await host.run(RunRequest("finish the task", session))
        assert completed.status == "completed"
        events_before = host.store.run_events(completed.run_id)
    finally:
        await host.close()

    facts = reopen_store(host)
    try:
        assert facts.run_events(completed.run_id) == events_before
        assert facts.service_shutdown_runs() == []
        # A close with nothing unfinished records no shutdown intent at all.
        assert not any(
            e["type"] == "host.shutdown_started"
            for e in facts.session_events(session)
        )
    finally:
        facts.close()

    restarted = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        await restarted.initialize()
        assert continuation_runs(restarted.store, completed.run_id) == []
    finally:
        await restarted.close()


async def test_budget_parked_run_is_not_resumed_by_shutdown(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.store.append_event(old_run, "run.interrupted", {
            "error": "Run budget exhausted", "reason": "token_budget",
        })
    finally:
        await host.close()

    facts = reopen_store(host)
    assert facts.service_shutdown_runs() == []
    facts.close()

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        assert continuation_runs(restarted.store, old_run) == []
    finally:
        await restarted.close()


async def test_second_crash_after_shutdown_claim_keeps_single_successor(
    isolated_workspace,
):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
    finally:
        await host.close()

    second = RuntimeHost(settings_for(isolated_workspace))
    claims = claimed(second.store, session)
    successor = claims[0]["payload"]["resumed_as"]
    second._lease.close()
    second.store.close()

    third = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}])
    )
    try:
        await third.initialize()
        assert continuation_runs(third.store, old_run) == [successor]
        assert len(claimed(third.store, session)) == 1
        assert await wait_terminal(third.store, successor) == "run.completed"
    finally:
        await third.close()


async def test_claim_refuses_other_terminals_and_user_stops(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        stopped = await host.admit_run(RunRequest("finish the task", session))
        await host.cancel_run(stopped)
    finally:
        await host.close()

    facts = reopen_store(host)
    try:
        assert facts.claim_run_recovery(
            session, stopped, "run_should_not_exist",
            finalize_payload={}, claim_payload={"source_run_id": stopped},
            terminal_claim=True,
        ) is False
        assert facts.service_shutdown_runs() == []
        completed = "run_completed_placeholder0"
        facts.create_run(completed, session)
        facts.append_event(completed, "run.completed", {"output": "done", "steps": 0})
        assert facts.claim_run_recovery(
            session, completed, "run_should_not_exist_2",
            finalize_payload={}, claim_payload={"source_run_id": completed},
            terminal_claim=True,
        ) is False
        assert continuation_runs(facts, stopped) == []
        assert continuation_runs(facts, completed) == []
    finally:
        facts.close()


def test_recovered_shutdown_successor_handles_new_web_approval(isolated_workspace):
    from fastapi.testclient import TestClient

    from eventide.api import create_app

    repo = make_repo(isolated_workspace / "repo")

    async def setup() -> str:
        host = RuntimeHost(settings_for(isolated_workspace))
        try:
            workspace = host.resolve_or_register_workspace(repo)
            session = host.create_session(workspace_id=workspace.workspace_id)
            return await host.admit_run(RunRequest("finish task", session))
        finally:
            await host.close()

    old_run = asyncio.run(setup())
    restarted = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"tool_calls": [
            {"id": "ask", "name": "bash", "arguments": {"command": "rm scratch.txt"}}
        ]},
        {"text": "done"},
    ]))
    with TestClient(create_app(restarted)) as client:
        deadline = time.monotonic() + 5
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = restarted.store.run_events(successor)
            if any(e["type"] == "run.completed" for e in events):
                break
            time.sleep(0.02)
        assert any(e["type"] == "run.completed" for e in events)


async def test_new_run_after_shutdown_history_cancels_normally(isolated_workspace):
    """A later Run in the same session must not inherit the earlier shutdown."""
    repo = make_repo(isolated_workspace / "repo")
    provider = BlockedModelProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.start_admitted_run(old_run)
        await wait_model_started(provider)
    finally:
        await host.close()

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "resumed output"}])
    )
    try:
        await restarted.initialize()
        assert await wait_terminal(restarted.store, old_run) == "run.interrupted"
        successor = continuation_runs(restarted.store, old_run)[0]
        assert await wait_terminal(restarted.store, successor) == "run.completed"
        # A brand-new Run in the same session, cancelled while queued: it has
        # no marker of its own, so this is a plain user cancel.
        queued = await restarted.admit_run(RunRequest("another task", session))
        record = await restarted.cancel_run(queued)
        assert record["status"] == "interrupted"
        payload = next(
            e["payload"] for e in restarted.store.run_events(queued)
            if e["type"] == "run.interrupted"
        )
        assert payload["reason"] == "cancelled"
        assert "resumable" not in payload
        assert restarted.store.service_shutdown_runs() == []
    finally:
        await restarted.close()

    facts = reopen_store(restarted)
    try:
        assert facts.service_shutdown_runs() == []
    finally:
        facts.close()

    final = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await final.initialize()
        # Neither the recovered chain nor the normally cancelled Run revives.
        assert continuation_runs(final.store, old_run) == [successor]
        assert continuation_runs(final.store, queued) == []
    finally:
        await final.close()


async def test_shutdown_recovery_isolated_per_session(isolated_workspace):
    """Two Sessions shut down by one close resume independently, never cross-bound."""
    first_repo = make_repo(isolated_workspace / "repo_a")
    second_repo = make_repo(isolated_workspace / "repo_b")
    provider = BlockedModelProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        first_ws = host.resolve_or_register_workspace(first_repo)
        second_ws = host.resolve_or_register_workspace(second_repo)
        session_a = host.create_session(workspace_id=first_ws.workspace_id)
        session_b = host.create_session(workspace_id=second_ws.workspace_id)
        run_a = await host.admit_run(RunRequest("task a", session_a))
        run_b = await host.admit_run(RunRequest("task b", session_b))
        host.start_admitted_run(run_a)
        host.start_admitted_run(run_b)
        await wait_model_started(provider)
    finally:
        await host.close()

    facts = reopen_store(host)
    try:
        markers = [
            e for e in facts.session_events(session_a) + facts.session_events(session_b)
            if e["type"] == "host.shutdown_started"
        ]
        # One close marks both Runs; each marker names its exact Run.
        assert sorted(m["payload"]["run_id"] for m in markers) == sorted([run_a, run_b])
        assert len({m["payload"]["shutdown_id"] for m in markers}) == 1
    finally:
        facts.close()

    restarted = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([{"text": "a done"}, {"text": "b done"}]),
    )
    try:
        await restarted.initialize()
        successor_a = [
            e for e in restarted.store.session_events(session_a)
            if e["type"] == "recovery.claimed"
        ]
        successor_b = [
            e for e in restarted.store.session_events(session_b)
            if e["type"] == "recovery.claimed"
        ]
        assert [e["payload"]["source_run_id"] for e in successor_a] == [run_a]
        assert [e["payload"]["source_run_id"] for e in successor_b] == [run_b]
        resumed_a = successor_a[0]["payload"]["resumed_as"]
        resumed_b = successor_b[0]["payload"]["resumed_as"]
        assert await wait_terminal(restarted.store, resumed_a) == "run.completed"
        assert await wait_terminal(restarted.store, resumed_b) == "run.completed"
    finally:
        await restarted.close()


async def test_user_stop_after_claim_blocks_dispatch(isolated_workspace):
    """A stop recorded after the claim but before initialize parks the recovery."""
    repo = make_repo(isolated_workspace / "repo")
    provider = BlockedModelProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.start_admitted_run(old_run)
        await wait_model_started(provider)
    finally:
        await host.close()

    # Construction claims the recovery; the user then stops the parked Run.
    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        record = await restarted.cancel_run(old_run)
        assert record["status"] == "interrupted"
        assert any(
            e["type"] == "run.cancel_requested"
            and e["payload"].get("run_id") == old_run
            for e in restarted.store.session_events(session)
        )
        await restarted.initialize()
        assert continuation_runs(restarted.store, old_run) == []
        blocked = [
            e for e in restarted.store.session_events(session)
            if e["type"] == "recovery.blocked"
        ]
        assert [e["payload"]["reason"] for e in blocked] == ["user_cancelled"]
        from eventide.recovery_center import build_recovery_view
        cut = restarted.store.recovery_snapshot(old_run)
        view = build_recovery_view(cut, {"latest_run_id": old_run})
        assert view["stop"]["kind"] == "user_cancel"
        assert len(view["recovery"]["claims"]) == 1
        assert cut["high_water"] >= blocked[0]["session_seq"]
    finally:
        await restarted.close()

    final = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await final.initialize()
        # Preserve the original claim; a user stop prevents dispatch, not history.
        assert continuation_runs(final.store, old_run) == []
        claims = claimed(final.store, session)
        assert len(claims) == 1
        assert claims[0]["payload"]["source_run_id"] == old_run
    finally:
        await final.close()


async def test_repeated_graceful_cycles_chain_single_successors(isolated_workspace):
    """Shutdown -> resume -> shutdown -> resume keeps one successor per Run."""
    repo = make_repo(isolated_workspace / "repo")
    first = ScriptedThenBlockingProvider()
    host = RuntimeHost(settings_for(isolated_workspace), first)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        run_a = await host.admit_run(RunRequest("finish the task", session))
        host.start_admitted_run(run_a)
        await wait_model_started(first)
    finally:
        await host.close()

    second_provider = ScriptedThenBlockingProvider()
    second = RuntimeHost(settings_for(isolated_workspace), second_provider)
    try:
        await second.initialize()
        run_b = continuation_runs(second.store, run_a)[0]
        await wait_model_started(second_provider)  # run_b is mid-model when closed
    finally:
        await second.close()

    facts = reopen_store(second)
    try:
        terminal_b = shutdown_terminal(facts, run_b)
        assert terminal_b is not None and terminal_b["payload"]["resumable"] is True
        # Run a's own terminal is untouched by the second cycle.
        assert len([
            e for e in facts.run_events(run_a) if e["type"] == "run.interrupted"
        ]) == 1
    finally:
        facts.close()

    third = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}])
    )
    try:
        await third.initialize()
        run_c = continuation_runs(third.store, run_b)[0]
        assert continuation_runs(third.store, run_a) == [run_b]
        assert len(claimed(third.store, session)) == 2
        sources = sorted(
            e["payload"]["source_run_id"] for e in claimed(third.store, session)
        )
        assert sources == sorted([run_a, run_b])
        assert await wait_terminal(third.store, run_c) == "run.completed"
        assert third.store.service_shutdown_runs() == []
    finally:
        await third.close()


async def test_shutdown_marker_failure_is_visible_and_runs_stay_cancelled(
    isolated_workspace, monkeypatch,
):
    """A failed shutdown write raises from close(); nothing pretends recoverable."""
    repo = make_repo(isolated_workspace / "repo")
    provider = BlockedModelProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.start_admitted_run(old_run)
        await wait_model_started(provider)

        def broken(session_id: str, run_id: str, shutdown_id: str) -> bool:
            raise RuntimeError("disk full while recording shutdown")

        monkeypatch.setattr(host.store, "mark_shutdown_started", broken)
        with pytest.raises(RuntimeError, match="disk full"):
            await host.close()
    finally:
        monkeypatch.undo()

    facts = reopen_store(host)
    try:
        terminal = shutdown_terminal(facts, old_run)
        # No marker landed, so the Run keeps a plain cancelled terminal and
        # stays parked; the failure surfaced instead of faking a shutdown.
        assert terminal is None
        plain = next(
            e for e in facts.run_events(old_run) if e["type"] == "run.interrupted"
        )
        assert plain["payload"]["reason"] == "cancelled"
        assert facts.service_shutdown_runs() == []
    finally:
        facts.close()

    restarted = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "nope"}])
    )
    try:
        await restarted.initialize()
        assert continuation_runs(restarted.store, old_run) == []
    finally:
        await restarted.close()


async def test_user_stop_persist_failure_cancels_nothing(
    isolated_workspace, monkeypatch,
):
    """An unrecordable stop raises and leaves the Run running; no fake success."""
    repo = make_repo(isolated_workspace / "repo")
    provider = BlockedModelProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        old_run = await host.admit_run(RunRequest("finish the task", session))
        host.start_admitted_run(old_run)
        await wait_model_started(provider)

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("disk full while recording user stop")

        monkeypatch.setattr(host.store, "append_fact", broken)
        with pytest.raises(RuntimeError, match="disk full"):
            await host.cancel_run(old_run)
        # The Run was not cancelled behind the failed stop.
        assert not host.store.run_is_terminal(old_run)
    finally:
        monkeypatch.undo()

    record = await host.cancel_run(old_run)
    assert record["status"] == "interrupted"
    await host.close()

    facts = reopen_store(host)
    try:
        plain = next(
            e for e in facts.run_events(old_run) if e["type"] == "run.interrupted"
        )
        assert plain["payload"]["reason"] == "cancelled"
        assert facts.service_shutdown_runs() == []
    finally:
        facts.close()


async def test_user_stop_during_recovery_admission_prevents_model_dispatch(
    isolated_workspace, monkeypatch,
):
    repo = make_repo(isolated_workspace / "repo")
    first = RuntimeHost(settings_for(isolated_workspace))
    workspace = first.resolve_or_register_workspace(repo)
    session = first.create_session(workspace_id=workspace.workspace_id)
    old_run = await first.admit_run(RunRequest("finish task", session))
    await first.close()
    provider = ScriptedProvider([{"text": "must not execute"}])
    restarted = RuntimeHost(settings_for(isolated_workspace), provider)
    original = restarted.admit_continue
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def paused(*args, **kwargs):
        entered.set()
        await proceed.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(restarted, "admit_continue", paused)
    initializing = asyncio.create_task(restarted.initialize())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await restarted.cancel_run(old_run)
        proceed.set()
        await initializing
        successor = continuation_runs(restarted.store, old_run)[0]
        assert restarted.store.run_is_terminal(successor)
        assert provider.requests == []
        assert not any(e["type"] == "model.attempt_started"
                       for e in restarted.store.run_events(successor))
        assert any(e["payload"].get("reason") == "cancelled"
                   for e in restarted.store.run_events(successor))
        assert len(claimed(restarted.store, session)) == 1
        with pytest.raises(ValueError, match="successor"):
            await restarted.cancel_run(old_run)
    finally:
        proceed.set()
        await initializing
        await restarted.close()
