"""Session metadata writes reject stale tabs and commit replay receipts atomically."""

import uuid

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_delivery_runtime import delivery_host


def test_replay_stale_tab_archive_and_delete_after_restart(tmp_path):
    host = delivery_host(tmp_path)
    settings = host.settings
    with TestClient(create_app(host)) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        path = f"/api/sessions/{session}"
        before = client.get(path).json()
        body = {"title": "Renamed", "expected_metadata_hash": before["metadata_hash"]}
        assert client.patch(path, json=body).status_code == 422
        assert (
            client.patch(
                path,
                json=body,
                headers={"Origin": "https://evil.invalid", "Idempotency-Key": "rename"},
            ).status_code
            == 403
        )
        response = client.patch(path, json=body, headers={"Idempotency-Key": "rename"})
        assert response.status_code == 200 and response.json()["title"] == "Renamed"
        renamed = response.json()
        stale = client.patch(
            path,
            json={"archived": True, "expected_metadata_hash": before["metadata_hash"]},
            headers={"Idempotency-Key": "stale"},
        )
        assert (
            stale.status_code == 409 and stale.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        )
        assert (
            client.patch(path, json=body, headers={"Idempotency-Key": "rename"}).json() == renamed
        )
        archive = client.patch(
            path,
            json={"archived": True, "expected_metadata_hash": renamed["metadata_hash"]},
            headers={"Idempotency-Key": "archive"},
        ).json()
        assert archive["archived"] is True
        assert client.get(path).json()["archived"] is True
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    with TestClient(create_app(restored)) as client:
        assert (
            client.patch(path, json=body, headers={"Idempotency-Key": "rename"}).json() == renamed
        )
        assert client.get(path).json()["archived"] is True
        delete = {"expected_metadata_hash": archive["metadata_hash"]}
        deleted = client.request("DELETE", path, json=delete, headers={"Idempotency-Key": "delete"})
        assert deleted.json() == {"deleted": session}
        assert client.get(path).status_code == 404
        assert (
            client.request(
                "DELETE", path, json=delete, headers={"Idempotency-Key": "delete"}
            ).json()
            == deleted.json()
        )
        restored.create_session(session)
        assert (
            client.request(
                "DELETE", path, json=delete, headers={"Idempotency-Key": "delete"}
            ).json()
            == deleted.json()
        )
        assert client.get(path).status_code == 200
        assert (
            client.request(
                "DELETE", path, json=delete, headers={"Idempotency-Key": "new-delete"}
            ).status_code
            == 409
        )


def test_history_and_control_session_cannot_be_deleted_or_relabelled(tmp_path):
    host = delivery_host(tmp_path)
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        host.store.append_fact(
            session, "message.user", {"message": {"role": "user", "content": "x"}}
        )
        path = f"/api/sessions/{session}"
        body = {"expected_metadata_hash": client.get(path).json()["metadata_hash"]}
        assert (
            client.request(
                "DELETE", path, json=body, headers={"Idempotency-Key": "delete"}
            ).status_code
            == 409
        )
        owner = host.configuration_owner()
        value = client.get(f"/api/sessions/{owner}").json()
        assert value["managed"] is True
        assert (
            client.patch(
                f"/api/sessions/{owner}",
                json={"title": "hijack", "expected_metadata_hash": value["metadata_hash"]},
                headers={"Idempotency-Key": "rename-control"},
            ).status_code
            == 409
        )


def test_failed_transaction_rolls_back_metadata_and_receipt(tmp_path, monkeypatch):
    host = delivery_host(tmp_path)
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        path = f"/api/sessions/{session}"
        before = client.get(path).json()
        original = host.store._append_fact_locked

        def fail(owner, kind, payload, **kwargs):
            if kind == "http.mutation_completed":
                raise RuntimeError("fixture storage failure")
            return original(owner, kind, payload, **kwargs)

        monkeypatch.setattr(host.store, "_append_fact_locked", fail)
        response = client.patch(
            path,
            json={"title": "should rollback", "expected_metadata_hash": before["metadata_hash"]},
            headers={"Idempotency-Key": "uncertain"},
        )
        assert response.status_code == 503
        assert "X-Eventide-Mutation-Outcome" not in response.headers
        assert client.get(path).json() == before
        monkeypatch.setattr(host.store, "_append_fact_locked", original)
        repeated = client.patch(
            path,
            json={"title": "should rollback", "expected_metadata_hash": before["metadata_hash"]},
            headers={"Idempotency-Key": "uncertain"},
        )
        assert repeated.status_code == 409
        assert "X-Eventide-Mutation-Outcome" not in repeated.headers
        assert client.get(path).json() == before


def test_creation_receipt_survives_restart_and_deleted_session(tmp_path):
    host = delivery_host(tmp_path)
    settings = host.settings
    headers = {"Idempotency-Key": "create-once"}
    with TestClient(create_app(host)) as client:
        assert client.post("/api/sessions").status_code == 422
        result = client.post("/api/sessions", json={}, headers=headers)
        assert result.status_code == 201
        identity = result.json()["session_id"]
        assert client.post("/api/sessions", headers=headers).json() == result.json()
        assert client.post(
            "/api/sessions", json={"working_directory": "."}, headers=headers
        ).status_code == 409
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    with TestClient(create_app(restored)) as client:
        assert client.post("/api/sessions", headers=headers).json() == result.json()
        metadata = client.get(f"/api/sessions/{identity}").json()
        assert client.request(
            "DELETE", f"/api/sessions/{identity}",
            json={"expected_metadata_hash": metadata["metadata_hash"]},
            headers={"Idempotency-Key": "delete-created"},
        ).status_code == 200
        assert client.post("/api/sessions", headers=headers).json() == result.json()
        assert client.get(f"/api/sessions/{identity}").status_code == 404


def test_creation_receipt_failure_rolls_back_session(tmp_path, monkeypatch):
    host = delivery_host(tmp_path)
    with TestClient(create_app(host)) as client:
        host.configuration_owner()
        before = host.store.list_sessions(host.default_workspace.workspace_id)
        original = host.store._append_fact_locked

        def fail(owner, kind, payload, **kwargs):
            if kind == "http.mutation_completed":
                raise RuntimeError("storage failure")
            return original(owner, kind, payload, **kwargs)

        monkeypatch.setattr(host.store, "_append_fact_locked", fail)
        headers = {"Idempotency-Key": "uncertain-create"}
        assert client.post("/api/sessions", headers=headers).status_code == 503
        assert host.store.list_sessions(host.default_workspace.workspace_id) == before
        monkeypatch.setattr(host.store, "_append_fact_locked", original)
        assert client.post("/api/sessions", headers=headers).status_code == 409
        assert host.store.list_sessions(host.default_workspace.workspace_id) == before
