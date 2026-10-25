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
