"""Reconnect reads committed events without replaying model or tool work."""

import uuid

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


def test_last_event_id_resumes_newer_than_initial_query(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(host)) as client:
        session = client.post(
            "/api/sessions",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["session_id"]
        run = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        first = client.get(f"/api/runs/{run}/events")
        assert first.status_code == 200
        ids = [int(line[4:]) for line in first.text.splitlines() if line.startswith("id: ")]
        assert len(ids) > 2
        before = host.store.run_events(run)
        replay = client.get(
            f"/api/runs/{run}/events?after={ids[0]}",
            headers={"Last-Event-ID": str(ids[-2])},
        )
        assert replay.status_code == 200
        assert [
            int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")
        ] == ids[-1:]
        assert replay.headers["cache-control"] == "no-cache"
        assert host.store.run_events(run) == before
        assert (
            client.get(f"/api/runs/{run}/events", headers={"Last-Event-ID": "invalid"}).status_code
            == 422
        )
        assert client.get(f"/api/runs/{run}/events?after=-1").status_code == 422
        assert (
            client.get(f"/api/runs/{run}/events", headers={"Last-Event-ID": "-1"}).status_code
            == 422
        )
