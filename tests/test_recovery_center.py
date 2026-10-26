"""MK-08F recovery center: readable recovery projection over real EventLog facts.

The HTTP layer is pure query: every scenario asserts the runtime event count is
unchanged by reading the projection, claims stay unique, and blocked facts are
session events bound to one source run.
"""

import asyncio
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for

POLICY = {"auto_resume": True, "version": 1}


def event_count(host: RuntimeHost) -> int:
    with host.store._lock:
        return host.store._connection.execute(
            "SELECT COUNT(*) FROM runtime_events"
        ).fetchone()[0]


def crash(host: RuntimeHost) -> None:
    host._lease.close()
    host.store.close()


def admit_then_crash(settings, repo: Path, prompt: str) -> str:
    async def phase() -> str:
        host = RuntimeHost(settings)
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        run_id = await host.admit_run(RunRequest(prompt, session))
        assert run_id
        crash(host)
        return run_id

    return asyncio.run(phase())


def test_claimed_run_shows_unique_successor_and_reading_is_pure(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = settings_for(isolated_workspace)
    old_run = admit_then_crash(settings, repo, "finish the task")

    restarted = RuntimeHost(settings, ScriptedProvider([{"text": "resumed output"}]))
    with TestClient(create_app(restarted)) as client:
        for _ in range(300):
            state = client.get(f"/api/runs/{old_run}/recovery")
            assert state.status_code == 200
            body = state.json()
            successor = body["recovery"]["successor_run_id"]
            if successor and body["recovery"]["successor"]["current"]:
                break
            time.sleep(0.02)
        assert successor, "claim must freeze exactly one successor"
        assert len(body["recovery"]["claims"]) == 1
        assert body["recovery"]["claims"][0]["resumed_as"] == successor
        assert body["stop"]["kind"] == "auto_recovered"
        assert body["recovery"]["history_available"] is True

        before = event_count(restarted)
        again = client.get(f"/api/runs/{old_run}/recovery").json()
        missing = client.get("/api/runs/r-does-not-exist/recovery")
        assert missing.status_code == 404
        assert event_count(restarted) == before, "recovery reading must not write facts"
        assert again["recovery"]["successor_run_id"] == successor
        assert again["recovery"]["claims"] == body["recovery"]["claims"]

        for _ in range(300):
            current = client.get(f"/api/runs/{old_run}/recovery").json()
            if current["recovery"]["successor"]["current"]["status"] == "completed":
                break
            time.sleep(0.02)
        assert current["recovery"]["successor"]["current"]["status"] == "completed"
        assert current["recovery"]["successor"]["admitted"] is True
        # The store invariant: the claim is the only successor of the old run.
        rows = restarted.store._connection.execute(
            "SELECT r.id FROM runs r JOIN turns t ON r.turn_id=t.id "
            "WHERE t.continuation_of=?",
            (old_run,),
        ).fetchall()
        assert [row["id"] for row in rows] == [successor]
    asyncio.run(restarted.close())


def test_blocked_facts_are_session_events_bound_to_source_run(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = settings_for(isolated_workspace)
    old_run = admit_then_crash(settings, repo, "blocked work")

    reopened = RuntimeHost(settings, ScriptedProvider([{"text": "resumed"}]))
    with TestClient(create_app(reopened)) as client:
        body = client.get(f"/api/runs/{old_run}/recovery").json()
        successor = body["recovery"]["successor_run_id"]
        assert successor
        session_id = body["session_id"]
        blocked_fact = reopened.store.append_fact(
            session_id,
            "recovery.blocked",
            {
                "source_run_id": successor,
                "resumed_as": "r-never-admitted",
                "reason": "model_config_changed",
            },
            author="runtime",
        )
        blocked_view = client.get(f"/api/runs/{successor}/recovery").json()
        assert blocked_view["high_water"] >= blocked_fact["session_seq"]
        assert blocked_view["stop"]["kind"] == "recovery_blocked"
        assert [
            (item["reason"], item["resumed_as"]) for item in blocked_view["recovery"]["blocked"]
        ] == [("model_config_changed", "r-never-admitted")]
        # The same session fact must not leak into the original run's view.
        source_view = client.get(f"/api/runs/{old_run}/recovery").json()
        assert source_view["recovery"]["blocked"] == []
    asyncio.run(reopened.close())


def test_stop_budget_and_unknown_kinds_and_no_auto_retry_action(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace))
    with TestClient(create_app(host)) as client:
        session_id = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]

        def craft_run(prompt: str) -> str:
            run_id = f"r-craft-{uuid.uuid4().hex[:8]}"
            # Facts go through the real EventLog admission path; nothing is
            # dispatched, so the run stays exactly as the facts describe it.
            host.store.admit_run(
                run_id,
                session_id,
                [
                    (
                        "run.admitted",
                        {"prompt_length": len(prompt), "recovery_policy": POLICY},
                        "user",
                        "user",
                    ),
                    ("run.started", {}, "system", "runtime"),
                ],
            )
            return run_id

        # User stop: interrupted by cancel; no automatic retry action may exist.
        run_id = craft_run("work")
        host.store.append_event(
            run_id,
            "run.interrupted",
            {"error": "Run cancelled before execution", "reason": "cancelled"},
        )
        body = client.get(f"/api/runs/{run_id}/recovery").json()
        assert body["stop"]["kind"] == "user_cancel"
        assert body["is_terminal"] is True
        assert body["available_actions"]["stop"]["available"] is False
        assert body["available_actions"]["continue"]["available"] is True
        assert set(body["available_actions"]) == {"stop", "continue"}

        # Budget park stays manually continuable, but never auto-retryable.
        budget_run = craft_run("more work")
        host.store.append_event(
            budget_run,
            "run.interrupted",
            {"error": "Token budget reached", "reason": "token_budget"},
        )
        body = client.get(f"/api/runs/{budget_run}/recovery").json()
        assert body["stop"]["kind"] == "budget"
        assert set(body["available_actions"]) == {"stop", "continue"}
        assert "auto_resume" not in body["available_actions"]
        assert "retry" not in body["available_actions"]

        # Unknown outcome must not advertise Continue as recoverable.
        unknown_run = craft_run("risky work")
        host.store.append_event(
            unknown_run,
            "tool.prepared",
            {"call_id": "c1", "name": "bash", "readonly": False},
        )
        host.store.append_event(
            unknown_run,
            "tool.outcome_unknown",
            {"call_id": "c1", "name": "bash", "error": "cleanup unconfirmed"},
        )
        host.store.append_event(
            unknown_run,
            "run.interrupted",
            {"error": "Tool outcome unknown", "reason": "unknown_tool_outcome"},
        )
        body = client.get(f"/api/runs/{unknown_run}/recovery").json()
        assert body["stop"]["kind"] == "unknown_outcome"
        assert [
            {key: value for key, value in item.items() if key != "at"}
            for item in body["unknown_tools"]
        ] == [{"call_id": "c1", "name": "bash", "error": "cleanup unconfirmed"}]
        assert body["available_actions"]["continue"]["available"] is False
        assert (
            body["available_actions"]["continue"]["reason"]
            == "unknown_tool_outcome_requires_manual_review"
        )

        before = event_count(host)
        for run in (run_id, budget_run, unknown_run):
            client.get(f"/api/runs/{run}/recovery")
        assert event_count(host) == before
    asyncio.run(host.close())


def test_history_without_frozen_policy_is_explicit(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace))
    with TestClient(create_app(host)) as client:
        session_id = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        # Pre-MK-08C history: a terminal run whose admission has no recovery policy.
        legacy_run = "r-legacy-history"
        host.store.admit_run(
            legacy_run,
            session_id,
            [
                ("run.admitted", {"prompt_length": 4}, "user", "user"),
                ("run.started", {}, "system", "runtime"),
                ("run.completed", {"output": "old answer"}, "assistant", "runtime"),
            ],
        )
        body = client.get(f"/api/runs/{legacy_run}/recovery").json()
        assert body["recovery"]["history_available"] is False
        assert body["recovery"]["frozen_policy"] is None
        assert "admission_predates_frozen_recovery_policy" in body["notes"]
        assert body["stop"]["kind"] == "completed"
        assert body["available_actions"]["continue"]["available"] is False
        assert body["recovery"]["successor_run_id"] is None
    asyncio.run(host.close())
