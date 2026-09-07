"""FastAPI session, run, event, and UI tests."""

import time

from fastapi.testclient import TestClient

from nexus_agent.api import create_app
from nexus_agent.providers import ScriptedProvider
from nexus_agent.runtime import AgentRuntime
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
        assert "NEXUS" in client.get("/").text
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
        assert configured["persistence"] == "process_memory_only"
        assert "local-test-secret" not in response.text
        assert runtime.settings.api_key == "local-test-secret"
