"""Public error compatibility and independent app ownership after route extraction."""

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_runtime import settings_for


@pytest.mark.parametrize(
    "method,path,body,status,code",
    [
        ("get", "/api/runs/missing", None, 404, "not_found"),
        ("get", "/api/sessions/missing", None, 404, "not_found"),
        ("get", "/api/workspaces/missing", None, 404, "not_found"),
        ("get", "/api/not-a-route", None, 404, "not_found"),
        ("post", "/healthz", None, 405, "method_not_allowed"),
        ("post", "/api/sessions/demo/runs", {"prompt": ""}, 422, "validation_error"),
    ],
)
def test_errors_keep_detail_and_expose_stable_code_message(
    isolated_workspace,
    method,
    path,
    body,
    status,
    code,
):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        response = client.request(method, path, json=body)
        assert response.status_code == status
        payload = response.json()
        assert payload["code"] == code
        assert payload["message"]
        if status == 422:
            assert isinstance(payload["detail"], list)
            assert payload["detail"][0]["loc"] == ["body", "prompt"]
        else:
            assert payload["message"] == payload["detail"]
        if status == 405:
            assert "GET" in response.headers["allow"]


def test_validation_error_never_echoes_candidate_credentials(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        response = client.put(
            "/api/config/provider",
            json={
                "provider": "unknown",
                "model": "",
                "api_key": {"key": "candidate-private-value"},
            },
        )
        assert response.status_code == 422
        assert "candidate-private-value" not in response.text
        for detail in response.json()["detail"]:
            assert {"loc", "msg", "type"} <= detail.keys()
            assert "input" not in detail and "ctx" not in detail


def test_origin_errors_use_the_same_envelope_without_mutation(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        response = client.post("/api/sessions", headers={"Origin": "https://attacker.invalid"})
        assert response.status_code == 403
        assert response.json()["code"] == "invalid_origin"
        assert response.json()["message"] == response.json()["detail"]
        assert host.store.list_sessions(host.default_workspace.workspace_id) == []


def test_http_error_redacts_known_keys_and_internal_errors_stay_generic(isolated_workspace):
    host = RuntimeHost(
        replace(settings_for(isolated_workspace), api_key="private-test-value"),
        ScriptedProvider([]),
    )

    def fail(_workspace):
        raise ValueError("invalid private-test-value")

    host.workspace_capabilities = fail
    with TestClient(create_app(host), raise_server_exceptions=False) as client:
        path = f"/api/workspaces/{host.default_workspace.workspace_id}/capabilities"
        response = client.get(path)
        assert response.status_code == 404
        assert "private-test-value" not in response.text

        def crash(_workspace):
            raise RuntimeError("internal private-test-value")

        host.workspace_capabilities = crash
        response = client.get(path)
        assert response.status_code == 500
        assert response.json() == {
            "code": "internal_error",
            "message": "Internal server error",
            "detail": "Internal server error",
        }


def test_openapi_describes_the_shared_error_contract(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        schema = client.get("/openapi.json").json()
        response = schema["paths"]["/api/sessions/{session_id}/runs"]["post"]["responses"]
        for status in ("403", "404", "409", "422", "500"):
            assert response[status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ErrorResponse",
            }


def test_two_apps_keep_runs_reservations_and_provider_mutations_independent(isolated_workspace):
    class WaitingProvider:
        async def complete(self, _request):
            await asyncio.Event().wait()

    left_dir = isolated_workspace / "left"
    right_dir = isolated_workspace / "right"
    left_dir.mkdir()
    right_dir.mkdir()
    left = RuntimeHost(settings_for(left_dir), WaitingProvider())
    right = RuntimeHost(settings_for(right_dir), ScriptedProvider([{"text": "right finished"}]))
    with TestClient(create_app(left)) as first, TestClient(create_app(right)) as second:
        left_session = first.post("/api/sessions").json()["session_id"]
        left_run = first.post(
            f"/api/sessions/{left_session}/runs",
            json={"prompt": "wait"},
        ).json()["run_id"]
        right_session = second.post("/api/sessions").json()["session_id"]
        right_run = second.post(
            f"/api/sessions/{right_session}/runs",
            json={"prompt": "finish"},
        ).json()["run_id"]
        assert wait_for_run(second, right_run)["output"] == "right finished"
        saved = second.put(
            "/api/config/provider",
            json={
                "provider": "openai_compatible",
                "model": "model",
                "api_key": "fake-test-key",
            },
        )
        assert saved.status_code == 200
        assert first.get(f"/api/runs/{right_run}").status_code == 404
        assert second.get(f"/api/runs/{left_run}").status_code == 404
        assert first.post(f"/api/runs/{left_run}/cancel").status_code == 200
        assert wait_for_run(first, left_run)["status"] == "interrupted"
