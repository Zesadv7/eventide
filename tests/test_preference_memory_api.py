"""Durable memory control HTTP receipts and local access boundaries."""

import uuid

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_runtime import settings_for


def headers():
    return {"Idempotency-Key": uuid.uuid4().hex}


def test_memory_crud_revision_retry_and_source_contract(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]), enable_mcp=False)
    with TestClient(create_app(host)) as client:
        assert client.get("/api/memory").json()["settings"] == {"enabled": True, "revision": 0}
        payload = {"scope": "global", "key": "style", "content": "Concise explanations"}
        key = headers()
        saved = client.post("/api/memory", json=payload, headers=key)
        assert saved.status_code == 201
        record = saved.json()["memory"]
        assert record["source"] == {"type": "manual", "event_ids": [], "session_id": None}
        assert client.post("/api/memory", json=payload, headers=key).json() == saved.json()
        assert (
            client.post(
                "/api/memory", json={**payload, "content": "Different"}, headers=key
            ).status_code
            == 409
        )
        conflict = client.post("/api/memory", json=payload, headers=headers())
        assert conflict.status_code == 409
        assert conflict.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        path = "/api/memory/" + record["id"]
        patch = client.patch(
            path, json={"expected_revision": 1, "content": "Detailed"}, headers=headers()
        )
        assert patch.json()["memory"]["revision"] == 2
        stale = client.patch(
            path, json={"expected_revision": 1, "status": "paused"}, headers=headers()
        )
        assert stale.status_code == 409
        assert stale.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        revoked = client.request("DELETE", path, json={"expected_revision": 2}, headers=headers())
        assert revoked.json()["memory"]["status"] == "revoked"
        assert client.get("/api/memory").json()["memories"] == []
        assert client.get("/api/memory?include_revoked=true").json()["memories"][0]["revision"] == 3
        assert (
            client.patch(
                path, json={"expected_revision": 3, "status": "active"}, headers=headers()
            ).status_code
            == 409
        )
        assert len(host.store.facts_by_type(("memory.record",))) == 3
        assert client.get("/api/runs/missing/memory").status_code == 404


def test_memory_settings_atomic_receipt_and_same_origin(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]), enable_mcp=False)
    with TestClient(create_app(host)) as client:
        body = {"expected_revision": 0, "enabled": False}
        key = headers()
        assert client.patch("/api/memory/settings", json=body).status_code == 422
        assert (
            client.patch(
                "/api/memory/settings", json=body, headers={**key, "Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        result = client.patch("/api/memory/settings", json=body, headers=key)
        assert result.json() == {"settings": {"enabled": False, "revision": 1}}
        assert client.patch("/api/memory/settings", json=body, headers=key).json() == result.json()
        assert client.patch("/api/memory/settings", json=body, headers=headers()).status_code == 409
        assert len(host.store.facts_by_type(("memory.settings",))) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "global", "key": "api_key", "content": "private-value"},
        {"scope": "global", "key": "language", "content": "password=private-value"},
        {"scope": "global", "key": "language", "content": "中文", "source": {"type": "user"}},
        {"scope": "project", "key": "language", "content": "中文"},
        {"scope": "global", "key": "language", "content": "中文", "project_id": "fake"},
    ],
)
def test_memory_bad_inputs_no_secret_memory_facts(isolated_workspace, payload):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]), enable_mcp=False)
    with TestClient(create_app(host)) as client:
        result = client.post("/api/memory", json=payload, headers=headers())
        assert result.status_code == 422
        assert not host.store.facts_by_type(("memory.record", "memory.suggestion"))
        assert "private-value" not in str(
            host.store.facts_by_type(("http.mutation_started", "http.mutation_failed"))
        )


def test_chat_suggestion_api_accept_and_receipts(isolated_workspace):
    host = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "ok"}] * 3), enable_mcp=False
    )
    with TestClient(create_app(host)) as client:
        session = client.post("/api/sessions", headers=headers()).json()["session_id"]
        for _ in range(2):
            result = client.post(
                f"/api/sessions/{session}/runs",
                json={"prompt": "偏好：language=中文"},
                headers=headers(),
            )
            assert result.status_code == 202
            run_id = result.json()["run_id"]
            wait_for_run(client, run_id)
        state = client.get(
            "/api/memory", params={"project_id": host.default_workspace.project_id}
        ).json()
        suggestion = state["suggestions"][0]
        details = client.get(f"/api/runs/{run_id}/memory").json()
        assert details["receipts"][0]["suggestion_id"] == suggestion["id"]
        assert details["injections"][0]["memories"] == []
        key = headers()
        accepted = client.post(
            f"/api/memory/suggestions/{suggestion['id']}/resolve",
            json={"expected_revision": 1, "action": "accept"},
            headers=key,
        )
        assert accepted.status_code == 200
        assert accepted.json()["memory"]["scope"] == "project"
        assert (
            client.post(
                f"/api/memory/suggestions/{suggestion['id']}/resolve",
                json={"expected_revision": 1, "action": "accept"},
                headers=key,
            ).json()
            == accepted.json()
        )
