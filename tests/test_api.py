"""FastAPI session, run, event, and UI tests."""

import time

from fastapi.testclient import TestClient

from eventide.api import _probe_error, create_app
from eventide.models import ModelResponse
from eventide.providers import ScriptedProvider
from eventide.runtime import AgentRuntime
from tests.test_runtime import settings_for


def wait_for_run(client: TestClient, run_id: str):
    for _ in range(100):
        response = client.get(f"/api/runs/{run_id}")
        if response.status_code == 200 and response.json()["status"] != "running":
            return response.json()
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_api_run_and_console(isolated_workspace):
    runtime = AgentRuntime(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "api done"}])
    )
    with TestClient(create_app(runtime)) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        console = client.get("/").text
        assert "EVENTIDE" in console
        assert "配置大模型" in console
        assert 'value="openai_responses"' in console
        assert "检查连接" in console
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "hello"})
        assert accepted.status_code == 202
        run = wait_for_run(client, accepted.json()["run_id"])
        assert run["output"] == "api done"
        events = runtime.store.get_events(run["id"])
        assert events[-1]["type"] == "run.completed"


def test_api_rejects_empty_prompt(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        response = client.post(f"/api/sessions/{session}/runs", json={"prompt": ""})
        assert response.status_code == 422


def test_api_sse_and_missing_resources(isolated_workspace):
    runtime = AgentRuntime(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "streamed"}])
    )
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "hello"})
        run_id = accepted.json()["run_id"]
        wait_for_run(client, run_id)
        events = client.get(f"/api/runs/{run_id}/events")
        assert events.status_code == 200
        assert "event: run.completed" in events.text
        assert client.get("/api/runs/missing").status_code == 404
        assert (
            client.post(
                f"/api/runs/{run_id}/approvals/missing", json={"approved": False}
            ).status_code
            == 404
        )


def test_api_resolves_pending_approval(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "danger", "name": "bash", "arguments": {"command": "rm x"}}]},
            {"text": "denial handled"},
        ]
    )
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "delete"})
        run_id = accepted.json()["run_id"]
        approval_id = None
        for _ in range(100):
            events = runtime.store.get_events(run_id)
            pending = [event for event in events if event["type"] == "approval.required"]
            if pending:
                approval_id = pending[0]["payload"]["approval_id"]
                break
            time.sleep(0.01)
        assert approval_id
        response = client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}", json={"approved": False}
        )
        assert response.status_code == 200
        assert wait_for_run(client, run_id)["output"] == "denial handled"


def test_api_configures_provider_without_echoing_secret(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(runtime)) as client:
        initial = client.get("/api/config/provider").json()
        assert initial["api_key_configured"] is False
        assert "api_key" not in initial
        response = client.put(
            "/api/config/provider",
            json={
                "provider": "openai_compatible",
                "api_key": "local-test-secret",
                "base_url": "https://model.example/v1",
                "model": "tool-model",
            },
        )
        assert response.status_code == 200
        configured = response.json()
        assert configured["api_key_configured"] is True
        assert configured["persistence"] == "sqlite_encrypted"
        assert "local-test-secret" not in response.text
        assert runtime.settings.api_key == "local-test-secret"
        assert configured["api_key_status"] == "configured"
        assert configured["api_key_source"] == "sqlite_encrypted"
        assert configured["updated_at"] is not None


def test_api_tests_candidate_config_and_returns_latency(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    captured = {}

    async def probe_provider(**kwargs):
        captured.update(kwargs)
        return ModelResponse("连接成功")

    runtime.probe_provider = probe_provider
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/config/provider/test",
            json={
                "provider": "openai_responses",
                "api_key": "candidate-secret",
                "base_url": "https://model.example/v1",
                "model": "response-model",
            },
        )
        result = response.json()
        assert response.status_code == 200
        assert result["success"] is True
        assert result["provider"] == "openai_responses"
        assert result["latency_ms"] >= 0
        assert result["message"] == "连接成功"
        assert captured["api_key"] == "candidate-secret"
        assert "candidate-secret" not in response.text


def test_api_probe_failure_is_stable_and_redacted(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))

    async def probe_provider(**_kwargs):
        raise RuntimeError("401 invalid api key: highly-secret-value")

    runtime.probe_provider = probe_provider
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/config/provider/test",
            json={"provider": "anthropic", "model": "model", "api_key": "highly-secret-value"},
        )
        result = response.json()
        assert result["success"] is False
        assert result["error_type"] == "authentication"
        assert result["detail"]
        assert "highly-secret-value" not in result["detail"]
        assert "highly-secret-value" not in response.text


def test_probe_error_classifies_by_exception_type():
    cases = {
        "AuthenticationError": "authentication",
        "PermissionDeniedError": "permission_denied",
        "NotFoundError": "model_not_found",
        "RateLimitError": "rate_limit",
        "BadRequestError": "bad_request",
        "APIConnectionError": "network",
        "TimeoutError": "timeout",
    }
    for class_name, expected in cases.items():
        outer = RuntimeError("wrapped")
        outer.__cause__ = type(class_name, (RuntimeError,), {})(class_name)
        assert _probe_error(outer)[0] == expected


def test_probe_error_string_fallback():
    assert _probe_error(RuntimeError("401 invalid api key"))[0] == "authentication"
    assert _probe_error(RuntimeError("model not found"))[0] == "model_not_found"
    assert _probe_error(RuntimeError("429 too many requests"))[0] == "rate_limit"
    assert _probe_error(RuntimeError("529 overloaded"))[0] == "overloaded"
    assert _probe_error(RuntimeError("connection refused"))[0] == "network"
    assert _probe_error(RuntimeError("ssl certificate verify failed"))[0] == "network"
    assert _probe_error(RuntimeError("no model api key configured"))[0] == "missing_api_key"
    assert _probe_error(RuntimeError("已保存的 API Key 无法解密"))[0] == "decrypt_error"


def test_api_clear_and_reset_persisted_config(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(runtime)) as client:
        saved = client.put(
            "/api/config/provider",
            json={"provider": "openai_responses", "model": "model", "api_key": "secret"},
        )
        assert saved.status_code == 200
        cleared = client.put(
            "/api/config/provider",
            json={
                "provider": "openai_responses",
                "model": "model",
                "clear_api_key": True,
            },
        )
        assert cleared.json()["api_key_configured"] is False
        reset = client.delete("/api/config/provider")
        assert reset.status_code == 200
        assert reset.json()["persistence"] == "environment"


def test_provider_mutations_are_loopback_only(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(runtime), client=("203.0.113.10", 50000)) as client:
        body = {"provider": "anthropic", "model": "model", "api_key": "secret"}
        assert client.put("/api/config/provider", json=body).status_code == 403
        assert client.post("/api/config/provider/test", json=body).status_code == 403
        assert client.delete("/api/config/provider").status_code == 403


def test_provider_save_waits_for_active_run(isolated_workspace):
    class SlowProvider:
        async def complete(self, _request):
            import asyncio

            await asyncio.sleep(0.2)
            return ModelResponse("done")

    runtime = AgentRuntime(settings_for(isolated_workspace), SlowProvider())
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "wait"})
        response = client.put(
            "/api/config/provider",
            json={"provider": "anthropic", "model": "model", "api_key": "secret"},
        )
        assert response.status_code == 409
        wait_for_run(client, accepted.json()["run_id"])


def test_api_cancels_active_run(isolated_workspace):
    class WaitingProvider:
        async def complete(self, _request):
            import asyncio

            await asyncio.Future()

    runtime = AgentRuntime(settings_for(isolated_workspace), WaitingProvider())
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "wait"})
        run_id = accepted.json()["run_id"]
        for _ in range(100):
            record = client.get(f"/api/runs/{run_id}")
            if record.status_code == 200:
                break
            time.sleep(0.01)
        cancelled = client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "interrupted"
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409
