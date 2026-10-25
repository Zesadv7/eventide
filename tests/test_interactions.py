"""In-run supplement persistence, one-time consumption, and late-arrival boundaries."""

import time
import uuid

from fastapi.testclient import TestClient

from eventide import interactions
from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


def wait_for_status(client: TestClient, run_id: str, statuses: set[str], timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/api/runs/{run_id}").json()
        if record.get("status") in statuses:
            return record
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {statuses}; last={record}")


def _content_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return ""


def _client(runtime: RuntimeHost) -> TestClient:
    return TestClient(create_app(runtime))


def _start_run(client: TestClient, prompt: str = "investigate") -> str:
    session = client.post(
        "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
    ).json()["session_id"]
    accepted = client.post(
        f"/api/sessions/{session}/runs",
        json={"prompt": prompt},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert accepted.status_code == 202, accepted.text
    return accepted.json()["run_id"]


def test_supplement_consumed_once_at_next_model_boundary(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([
            {
                "text": "still investigating", "delay_ms": 1500,
                "tool_calls": [{"id": "c1", "name": "read_file",
                                "arguments": {"path": "notes.txt"}}],
            },
            {"text": "done with supplement"},
        ]),
    )
    with _client(runtime) as client:
        run_id = _start_run(client)
        wait_for_status(client, run_id, {"running"})
        received = client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "also check the retry path", "body_version": 2},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert received.status_code == 200, received.text
        receipt = received.json()
        assert receipt["status"] == "pending"
        assert receipt["consumed"] is False

        final = wait_for_status(client, run_id, {"completed", "failed", "interrupted"})
        assert final["status"] == "completed"

        # The next model request after the boundary must contain exactly one
        # user message carrying the supplement; executed tools are not rewound.
        assert len(runtime.provider.requests) == 2
        second_messages = runtime.provider.requests[1].messages
        supplement_messages = [
            m for m in second_messages
            if m["role"] == "user"
            and _content_text(m).startswith(interactions.SUPPLEMENT_MESSAGE_PREFIX)
        ]
        assert len(supplement_messages) == 1
        assert "also check the retry path" in _content_text(supplement_messages[0])

        projected = client.get(f"/api/runs/{run_id}/supplements").json()
        assert projected["pending"] == 0
        assert len(projected["supplements"]) == 1
        record = projected["supplements"][0]
        assert record["status"] == "consumed"
        assert record["body_version"] == 2
        assert record["consumed_step"] == 2

        consumed_events = [
            e for e in runtime.store.get_events(run_id)
            if e["type"] == interactions.SUPPLEMENT_CONSUMED_EVENT
        ]
        assert len(consumed_events) == 1
        user_messages = [
            e for e in runtime.store.get_events(run_id)
            if e["type"] == "message.user"
            and _content_text(e["payload"]["message"]).startswith(
                interactions.SUPPLEMENT_MESSAGE_PREFIX
            )
        ]
        assert len(user_messages) == 1


def test_supplement_idempotent_replay_and_conflict(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([
            {"text": "working", "delay_ms": 1500},
            {"text": "done"},
        ]),
    )
    with _client(runtime) as client:
        run_id = _start_run(client)
        wait_for_status(client, run_id, {"running"})
        key = uuid.uuid4().hex
        first = client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "note the flaky test"},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 200
        replay = client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "note the flaky test"},
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        conflict = client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "different content"},
            headers={"Idempotency-Key": key},
        )
        assert conflict.status_code == 409

        wait_for_status(client, run_id, {"completed"})
        # A replay after the terminal outcome still returns the original receipt.
        late_replay = client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "note the flaky test"},
            headers={"Idempotency-Key": key},
        )
        assert late_replay.status_code == 200
        assert late_replay.json() == first.json()


def test_late_supplement_does_not_resume_or_leak_into_next_run(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([{"text": "quick done"}]),
    )
    with _client(runtime) as client:
        run_id = _start_run(client)
        wait_for_status(client, run_id, {"completed"})
        admitted_before = sum(
            1 for e in runtime.store.get_events(run_id) if e["type"] == "run.admitted"
        )

        late = client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "too late, do not resume"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert late.status_code == 200
        assert late.json()["status"] == "late"
        assert late.json()["consumed"] is False

        projected = client.get(f"/api/runs/{run_id}/supplements").json()
        assert projected["supplements"][0]["status"] == "late"
        assert projected["pending"] == 0

        events = runtime.store.get_events(run_id)
        assert all(
            e["type"] != interactions.SUPPLEMENT_CONSUMED_EVENT for e in events
        )
        assert not [
            e for e in events
            if e["type"] == "message.user"
            and _content_text(e["payload"]["message"]).startswith(
                interactions.SUPPLEMENT_MESSAGE_PREFIX
            )
        ]

        # A later run on the same session must not consume the other run's late
        # supplement, and no run is created automatically for it.
        next_run_id = _start_run(client, "fresh task")
        wait_for_status(client, next_run_id, {"completed"})
        assert client.get(f"/api/runs/{next_run_id}/supplements").json()["supplements"] == []
        next_messages = runtime.provider.requests[-1].messages
        assert not [
            m for m in next_messages
            if m["role"] == "user"
            and _content_text(m).startswith(interactions.SUPPLEMENT_MESSAGE_PREFIX)
        ]
        admitted_after = sum(
            1 for e in runtime.store.get_events(run_id) if e["type"] == "run.admitted"
        )
        assert admitted_after == admitted_before


def test_supplement_rejects_invalid_input_and_unknown_run(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}])
    )
    with _client(runtime) as client:
        run_id = _start_run(client)
        wait_for_status(client, run_id, {"completed"})
        assert client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": ""},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 422
        assert client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "x" * (interactions.SUPPLEMENT_BODY_LIMIT + 1)},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 422
        assert client.post(
            f"/api/runs/{run_id}/supplements",
            json={"body": "ok", "body_version": 0},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 422
        assert client.get("/api/runs/run_missing/supplements").status_code == 404
        assert client.post(
            "/api/runs/run_missing/supplements",
            json={"body": "ok"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 404
