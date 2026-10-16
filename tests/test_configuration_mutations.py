"""Durable settings receipts, compare-and-swap and transport/SQLite failures."""

import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for

URL = "/api/config/provider"
GENERATIONS = "/api/generation/defaults"
SECRET = "candidate-configuration-secret"


def provider_body(client, **changes):
    return {
        "provider": "openai_compatible", "model": "test-model", "api_key": SECRET,
        "expected_config_hash": client.get(URL).json()["config_hash"], **changes,
    }


def generation_body(client, path=GENERATIONS, **changes):
    return {
        "model": "test-model", "generation": {"max_output_tokens": 128},
        "expected_config_hash": client.get(path, params={"model": "test-model"}).json()[
            "config_hash"
        ], **changes,
    }


def write(client, path, body, key="configuration-1", method="PUT"):
    return client.request(method, path, json=body, headers={"Idempotency-Key": key})


def facts(host, kind):
    return [e for e in host.store.session_events(host.configuration_owner()) if e["type"] == kind]


def test_provider_save_reset_replay_and_restart(isolated_workspace):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        body = provider_body(client)
        saved = write(client, URL, body)
        assert saved.status_code == 200, saved.text
        assert write(client, URL, body).json() == saved.json()
        assert len(facts(host, "provider.configured")) == 1
        reset_body = {"expected_config_hash": saved.json()["config_hash"]}
        reset = write(client, URL, reset_body, "reset-1", "DELETE")
        assert reset.status_code == 200
        assert write(client, URL, body).json() == saved.json()  # historical response
        assert client.get(URL).json()["persistence"] == "environment"
        assert SECRET not in json.dumps(host.store.session_events(host.configuration_owner()))
    restored = RuntimeHost(settings)
    with TestClient(create_app(restored)) as client:
        assert write(client, URL, reset_body, "reset-1", "DELETE").json() == reset.json()
        assert write(client, URL, body).json() == saved.json()
        assert restored.settings.api_key is None
        assert len(facts(restored, "provider.configured")) == 2
    assert SECRET.encode() not in (settings.state_dir / "runtime.sqlite").read_bytes()


@pytest.mark.parametrize("path", [URL, GENERATIONS, "session"])
def test_stale_configuration_and_key_reuse_are_distinct(isolated_workspace, path):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        if path == "session":
            sid = client.post(
                "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).json()["session_id"]
            path = f"/api/sessions/{sid}/generation"
        body = provider_body(client) if path == URL else generation_body(client, path)
        assert write(client, path, body).status_code == 200
        conflict = write(client, path, {**body, "model": "changed-model"})
        assert conflict.status_code == 409
        assert "x-eventide-mutation-outcome" not in conflict.headers
        stale = write(client, path, body, "another-tab")
        assert stale.status_code == 409
        assert stale.headers["x-eventide-mutation-outcome"] == "rejected"
        assert write(client, path, body, "another-tab").json() == stale.json()
        assert write(client, path, body).status_code == 200


@pytest.mark.parametrize("path,method", [(URL, "PUT"), (URL, "DELETE"), (GENERATIONS, "PUT")])
def test_expected_hash_and_key_are_required(isolated_workspace, path, method):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        body = provider_body(client) if path == URL else generation_body(client)
        if method == "DELETE":
            body = {"expected_config_hash": body["expected_config_hash"]}
        assert client.request(method, path, json=body).status_code == 422
        missing_hash = {k: v for k, v in body.items() if k != "expected_config_hash"}
        assert write(client, path, missing_hash, method=method).status_code == 422
        assert not host.store.get_session("configuration_control")


def test_transport_close_failure_keeps_config_and_blocks_reuse(isolated_workspace):
    class Client:
        calls = 0

        async def close(self):
            self.calls += 1
            if self.calls == 1:
                raise ValueError(SECRET)

    transport = Client()
    host = RuntimeHost(settings_for(isolated_workspace), transport)
    with TestClient(create_app(host)) as client:
        before = host.settings
        response = write(client, URL, provider_body(client))
        assert response.status_code == 503 and SECRET not in response.text
        assert response.headers["x-eventide-mutation-outcome"] == "rejected"
        assert host.settings is before and host.store.get_provider_config() is None
        assert host.provider is transport
        assert client.get(URL).json()["client_status"] == "cleanup_required"
        sid = client.post("/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}).json()[
            "session_id"
        ]
        assert (
            client.post(
                f"/api/sessions/{sid}/runs",
                json={"prompt": "wait"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ).status_code
            == 409
        )
        recovered = write(client, URL, provider_body(client), "explicit-recovery")
        assert recovered.status_code == 200 and transport.calls == 2
        assert host.provider is None
        assert recovered.json()["client_status"] == "ready"


@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_storage_failure_reconciles_only_durable_commit(
    isolated_workspace, monkeypatch, after_commit, method,
):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        if method == "DELETE":
            assert write(client, URL, provider_body(client), "initial").status_code == 200
        before = host.settings
        record = host.store.get_provider_config()
        body = provider_body(client)
        if method == "DELETE":
            body = {"expected_config_hash": body["expected_config_hash"]}
        original = host.store.commit_configuration

        def uncertain(*args, **kwargs):
            if after_commit:
                original(*args, **kwargs)
            raise RuntimeError(SECRET)

        monkeypatch.setattr(host.store, "commit_configuration", uncertain)
        response = write(client, URL, body, method=method)
        assert response.status_code == 503 and SECRET not in response.text
        assert "x-eventide-mutation-outcome" not in response.headers
        monkeypatch.setattr(host.store, "commit_configuration", original)
        repeated = write(client, URL, body, method=method)
        assert repeated.status_code == (200 if after_commit else 409)
        if not after_commit:
            assert host.settings is before and host.store.get_provider_config() == record
        elif method == "PUT":
            assert host.settings.api_key == SECRET
        else:
            assert host.settings.api_key is None and host.store.get_provider_config() is None
        assert SECRET not in json.dumps(host.store.session_events(host.configuration_owner()))


def test_completion_append_failure_rolls_back_provider_and_generation(
    isolated_workspace, monkeypatch
):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        original = host.store._append_fact_locked

        def fail_completion(owner, kind, payload, **kwargs):
            if kind == "http.mutation_completed":
                raise RuntimeError(SECRET)
            return original(owner, kind, payload, **kwargs)

        monkeypatch.setattr(host.store, "_append_fact_locked", fail_completion)
        assert write(client, URL, provider_body(client)).status_code == 503
        assert host.store.get_provider_config() is None and host.settings.api_key is None
        assert not facts(host, "provider.configured")
        body = generation_body(client)
        assert write(client, GENERATIONS, body).status_code == 503
        assert client.get(GENERATIONS, params={"model": "test-model"}).json()["defaults"] == {}
        assert not facts(host, "generation.configured")


def test_generation_receipts_preserve_unset_null_and_empty_session_deletion(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        sid = client.post("/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}).json()[
            "session_id"
        ]
        path = f"/api/sessions/{sid}/generation"
        body = generation_body(client, path, generation={})
        response = write(client, path, body)
        assert response.status_code == 200
        conflict = write(client, path, {**body, "generation": {"temperature": None}})
        assert conflict.status_code == 409 and "different content" in conflict.text
        assert host.store.session_events(sid) == []
        assert host.is_control_session("configuration_control")
        assert (
            client.request(
                "DELETE",
                f"/api/sessions/{sid}",
                json={
                    "expected_metadata_hash": client.get(f"/api/sessions/{sid}").json()[
                        "metadata_hash"
                    ]
                },
                headers={"Idempotency-Key": "delete-empty"},
            ).status_code
            == 200
        )
        host.create_session(sid)
        assert host.generation_settings("test-model", sid)["session"] == {}
        assert write(client, path, body).status_code == 409
        run = client.post(
            "/api/sessions/configuration_control/runs",
            json={"prompt": "no"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert run.status_code == 422


def test_generation_saved_session_identity_does_not_survive_recreation(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        sid = client.post("/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}).json()[
            "session_id"
        ]
        path = f"/api/sessions/{sid}/generation"
        body = generation_body(client, path)
        assert write(client, path, body).json()["session"] == {"max_output_tokens": 128}
        assert (
            client.request(
                "DELETE",
                f"/api/sessions/{sid}",
                json={
                    "expected_metadata_hash": client.get(f"/api/sessions/{sid}").json()[
                        "metadata_hash"
                    ]
                },
                headers={"Idempotency-Key": "delete-empty"},
            ).status_code
            == 200
        )
        host.create_session(sid)
        assert host.generation_settings("test-model", sid)["session"] == {}


async def test_concurrent_config_changes_and_admission_wait_for_client_close(isolated_workspace):
    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        async def close(self):
            entered.set()
            await release.wait()

    host = RuntimeHost(settings_for(isolated_workspace), Client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(host)),
                                base_url="http://testserver") as client:
        state = (await client.get(URL)).json()
        body = {"provider": "anthropic", "model": "one", "api_key": SECRET,
                "expected_config_hash": state["config_hash"]}
        first = asyncio.create_task(client.put(URL, json=body,
                                               headers={"Idempotency-Key": "one"}))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            second = await client.put(URL, json=body, headers={"Idempotency-Key": "two"})
            assert second.status_code == 409
            assert second.headers["x-eventide-mutation-outcome"] == "rejected"
            duplicate = await client.put(URL, json=body, headers={"Idempotency-Key": "one"})
            assert duplicate.status_code == 409
            assert "x-eventide-mutation-outcome" not in duplicate.headers
            with pytest.raises(ValueError, match="configuration"):
                await host.probe_provider(
                    provider="anthropic", api_key=SECRET, base_url=None, model="one",
                )
            assert not host.store.facts_by_type(("model.attempt_started",))
            generation = host.generation_settings("test-model")
            rejected = await client.put(GENERATIONS, json={
                "model": "test-model", "generation": {},
                "expected_config_hash": generation["config_hash"],
            }, headers={"Idempotency-Key": "model"})
            assert rejected.status_code == 409
            with pytest.raises(ValueError, match="configuration"):
                await host.admit_run(RunRequest("wait", host.create_session()))
        finally:
            release.set()
            assert (await first).status_code == 200
    await host.close()
