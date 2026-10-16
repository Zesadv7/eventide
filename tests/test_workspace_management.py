"""Project metadata changes are durable and never remove source content."""

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_delivery_runtime import delivery_host


def test_register_remove_replay_and_restart(tmp_path):
    host = delivery_host(tmp_path)
    settings = host.settings
    root = tmp_path / "other"
    root.mkdir()
    (root / "keep.txt").write_text("retained")
    body = {"path": str(root)}
    key = {"Idempotency-Key": "register"}
    with TestClient(create_app(host)) as client:
        assert client.post("/api/workspaces", json=body).status_code == 422
        response = client.post("/api/workspaces", json=body, headers=key)
        assert response.status_code == 201
        record = response.json()
        assert client.post("/api/workspaces", json=body, headers=key).json() == record
        assert (
            client.post(
                "/api/workspaces",
                json={"path": str(tmp_path)},
                headers=key,
            ).status_code
            == 409
        )
        path = "/api/workspaces/" + record["workspace_id"]
        assert client.delete(path).status_code == 422
        assert client.delete(path, headers={"Idempotency-Key": "remove"}).status_code == 200
        assert client.post("/api/workspaces", json=body, headers=key).json() == record
        assert client.get(path).status_code == 404
        assert (root / "keep.txt").read_text() == "retained"
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    with TestClient(create_app(restored)) as client:
        assert client.delete(path, headers={"Idempotency-Key": "remove"}).status_code == 200
        replacement = client.post(
            "/api/workspaces",
            json=body,
            headers={"Idempotency-Key": "register-again"},
        ).json()
        assert replacement["workspace_id"] != record["workspace_id"]
        assert client.delete(path, headers={"Idempotency-Key": "remove"}).status_code == 200
        assert client.get("/api/workspaces/" + replacement["workspace_id"]).status_code == 200
        assert (
            client.delete(
                "/api/workspaces/" + restored.default_workspace.workspace_id,
                headers={"Idempotency-Key": "remove-startup"},
            ).status_code
            == 409
        )


def test_workspace_atomic_receipt_failure(tmp_path, monkeypatch):
    host = delivery_host(tmp_path)
    root = tmp_path / "other"
    root.mkdir()
    with TestClient(create_app(host)) as client:
        owner = host.configuration_owner()
        before = host.store.list_workspaces()
        original = host.store._append_fact_locked

        def fail(session, kind, payload, **kwargs):
            if session == owner and kind == "http.mutation_completed":
                raise RuntimeError("storage unavailable")
            return original(session, kind, payload, **kwargs)

        monkeypatch.setattr(host.store, "_append_fact_locked", fail)
        body = {"path": str(root)}
        key = {"Idempotency-Key": "unknown-register"}
        assert client.post("/api/workspaces", json=body, headers=key).status_code == 503
        assert host.store.list_workspaces() == before
        monkeypatch.setattr(host.store, "_append_fact_locked", original)
        assert client.post("/api/workspaces", json=body, headers=key).status_code == 409
        record = client.post(
            "/api/workspaces",
            json=body,
            headers={"Idempotency-Key": "new-register"},
        ).json()
        path = "/api/workspaces/" + record["workspace_id"]
        monkeypatch.setattr(host.store, "_append_fact_locked", fail)
        assert client.delete(path, headers={"Idempotency-Key": "unknown-remove"}).status_code == 503
        assert client.get(path).status_code == 200
