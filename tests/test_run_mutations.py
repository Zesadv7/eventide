"""User decisions have stable identities and target precise execution facts."""

import asyncio
import time

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_host import interrupted_host
from tests.test_runtime import settings_for


def test_missing_keys_cannot_create_session_or_change_run(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        for path, body in (
            ("/api/sessions/unknown/runs", {"prompt": "no"}),
            ("/api/sessions/unknown/continue", None),
            ("/api/sessions/unknown/abandon", {"expected_run_id": "old"}),
            ("/api/runs/unknown/cancel", None),
            ("/api/runs/unknown/approvals/one", {"approved": True}),
        ):
            assert client.post(path, json=body).status_code == 422
        assert host.store.get_session("unknown") is None
        assert host.provider.requests == []


def test_cancel_success_replays_after_restart_without_recanceling(isolated_workspace):
    class Waiting:
        async def complete(self, request):
            await asyncio.Future()

    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, Waiting())
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        run = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "wait"},
            headers={"Idempotency-Key": "start"},
        ).json()["run_id"]
        path = f"/api/runs/{run}/cancel"
        key = {"Idempotency-Key": "stop"}
        first = client.post(path, headers=key)
        assert first.status_code == 200
        assert client.post(path, headers=key).json() == first.json()
        assert client.post(path, headers={"Idempotency-Key": "new-stop"}).status_code == 409
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(reopened)) as client:
        assert client.post(path, headers=key).json() == first.json()
        assert reopened.provider.requests == []


def test_approval_replay_keeps_original_decision(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "danger", "name": "bash", "arguments": {"command": "rm x"}}]},
            {"text": "denial handled"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        run = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "delete"},
            headers={"Idempotency-Key": "start"},
        ).json()["run_id"]
        for _ in range(200):
            pending = client.get(f"/api/runs/{run}").json()["pending_approvals"]
            if pending:
                break
            time.sleep(0.01)
        assert pending
        approval = next(iter(pending))
        path = f"/api/runs/{run}/approvals/{approval}"
        key = {"Idempotency-Key": "decision"}
        assert client.post(path, json={"approved": "false"}, headers=key).status_code == 422
        first = client.post(path, json={"approved": False}, headers=key)
        assert first.status_code == 200
        wait_for_run(client, run)
        assert client.post(path, json={"approved": False}, headers=key).json() == first.json()
        assert client.post(path, json={"approved": True}, headers=key).status_code == 409
        assert len([e for e in host.store.run_events(run) if e["type"] == "approval.resolved"]) == 1


def test_abandon_targets_original_parked_run_and_replays(isolated_workspace):
    host, session, _ = asyncio.run(interrupted_host(isolated_workspace, "bash"))
    with TestClient(create_app(host)) as client:
        path = f"/api/sessions/{session}/abandon"
        wrong = client.post(
            path, json={"expected_run_id": "different"}, headers={"Idempotency-Key": "stale"}
        )
        assert wrong.status_code == 409
        assert wrong.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        assert host.store.latest_run(session)["id"] == "crashed"
        key = {"Idempotency-Key": "abandon"}
        first = client.post(path, json={"expected_run_id": "crashed"}, headers=key)
        assert first.status_code == 200
        assert (
            client.post(path, json={"expected_run_id": "crashed"}, headers=key).json()
            == first.json()
        )
        assert client.post(path, json={"expected_run_id": "new"}, headers=key).status_code == 409
        assert len(host.session_runs(session)) == 2
