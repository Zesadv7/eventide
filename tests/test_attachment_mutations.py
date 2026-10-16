"""Attachment transport retries never repeat filesystem side effects."""

import base64

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


def body(text="private attachment text"):
    return {"name": "notes.txt", "content_base64": base64.b64encode(text.encode()).decode()}


def test_upload_delete_replay_conflict_and_restart(isolated_workspace):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        path = f"/api/sessions/{session}/attachments"
        key = {"Idempotency-Key": "upload"}
        assert client.post(path, json=body()).status_code == 422
        assert (
            client.post(
                path, json=body(), headers={**key, "Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        original = client.post(path, json=body(), headers=key)
        assert original.status_code == 201
        assert client.post(path, json=body(), headers=key).json() == original.json()
        assert client.post(path, json=body("different"), headers=key).status_code == 409
        assert len(client.get(path).json()) == 1
        identity = original.json()["attachment_id"]
        delete_path = path + "/" + identity
        assert client.delete(delete_path).status_code == 422
        removed = client.delete(delete_path, headers={"Idempotency-Key": "delete"})
        assert removed.status_code == 200
        assert (
            client.delete(delete_path, headers={"Idempotency-Key": "delete"}).json()
            == removed.json()
        )
        assert client.post(path, json=body(), headers=key).json() == original.json()
        assert client.get(path).json() == []  # Replay does not resurrect a deleted file.
        facts = str(host.store.session_events(host.configuration_owner()))
        assert body()["content_base64"] not in facts
        assert "private attachment text" not in facts
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(reopened)) as client:
        assert client.post(path, json=body(), headers=key).json() == original.json()
        assert (
            client.delete(delete_path, headers={"Idempotency-Key": "delete"}).json()
            == removed.json()
        )
        assert client.get(path).json() == []
        assert reopened.provider.requests == []


@pytest.mark.parametrize("operation", ["upload", "delete"])
def test_receipt_failure_after_file_effect_never_replays(
    isolated_workspace, monkeypatch, operation
):
    import eventide.http_api.attachments as routes

    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        path = f"/api/sessions/{session}/attachments"
        if operation == "delete":
            created = client.post(path, json=body(), headers={"Idempotency-Key": "initial"}).json()
            path += "/" + created["attachment_id"]
        original_finish = routes.finish_mutation

        def fail(*args, **kwargs):
            raise OSError("private storage detail")

        monkeypatch.setattr(routes, "finish_mutation", fail)
        def send():
            return client.request(
                "POST" if operation == "upload" else "DELETE",
                path,
                json=body() if operation == "upload" else None,
                headers={"Idempotency-Key": "uncertain"},
            )
        first = send()
        assert first.status_code == 503
        assert "private storage detail" not in first.text
        monkeypatch.setattr(routes, "finish_mutation", original_finish)
        assert send().status_code == 409
        listed = client.get(f"/api/sessions/{session}/attachments").json()
        assert len(listed) == (1 if operation == "upload" else 0)
        assert host.provider.requests == []
