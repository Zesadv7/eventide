"""FastAPI session, run, event, and UI tests."""

import base64
import json
import time

from fastapi.testclient import TestClient

from eventide.api import _probe_error, create_app
from eventide.attachments import MAX_INLINE_BYTES, MAX_UPLOAD_BYTES
from eventide.models import ModelResponse
from eventide.providers import ScriptedProvider
from eventide.runtime import AgentRuntime
from tests.test_runtime import settings_for


def wait_for_run(client: TestClient, run_id: str):
    # Git checkpoint collection runs in a worker thread and can be delayed when the
    # full Windows suite is also exercising subprocess and thread cleanup.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        if response.status_code == 200 and response.json()["status"] != "running":
            return response.json()
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def encoded(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


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
        exported = client.get(f"/api/runs/{run_id}/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("application/x-ndjson")
        assert f"eventide-{run_id}.jsonl" in exported.headers["content-disposition"]
        facts = [json.loads(line) for line in exported.text.splitlines()]
        assert facts[-1]["type"] == "run.completed"
        assert "seq" not in facts[-1]
        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/runs/missing/export").status_code == 404
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


def test_api_session_status_exposes_task_plan(isolated_workspace):
    """GET /api/sessions/{id} carries the plan, active task, and evidence facts."""
    initial = [
        {"content": "Inspect", "status": "in_progress"},
        {"content": "Follow up", "status": "pending"},
    ]
    settled = [
        {"id": "t1", "content": "Inspect", "status": "completed", "summary": "Read the data file."},
        {"content": "Follow up", "status": "in_progress"},
    ]
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "plan", "name": "todo_write", "arguments": {"todos": initial}}]},
            {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "data"}}]},
            {"tool_calls": [{"id": "done", "name": "todo_write", "arguments": {"todos": settled}}]},
            {"text": "done"},
        ]
    )
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        (isolated_workspace / "data").write_text("x", encoding="utf-8")
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "plan"})
        wait_for_run(client, accepted.json()["run_id"])
        record = client.get(f"/api/sessions/{session}").json()
        assert record["active_task_id"] == "t2"
        tasks = record["task_state"]["tasks"]
        assert [task["id"] for task in tasks] == ["t1", "t2"]
        assert tasks[0]["status"] == "completed"
        assert tasks[0]["summary"] == "Read the data file."
        assert [entry["name"] for entry in tasks[0]["evidence"]] == ["read_file"]
        assert tasks[1]["status"] == "in_progress"
        assert record["task_plan"][0]["id"] == "t1"


def test_api_runtime_settings_exposes_budgets_without_secrets(isolated_workspace):
    runtime = AgentRuntime(
        settings_for(
            isolated_workspace,
            context_limit=1234,
            approval_timeout=7.5,
            max_steps=9,
            task_max_steps=4,
        ),
        ScriptedProvider([]),
    )
    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/runtime/settings")
        assert response.status_code == 200
        assert response.json() == {
            "context_limit": 1234,
            "approval_timeout": 7.5,
            "max_steps": 9,
            "task_max_steps": 4,
        }
        assert "api_key" not in response.text


def test_api_workspace_capabilities_empty_missing_and_broken_mcp_json(
    isolated_workspace,
):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(runtime)) as client:
        workspace_id = runtime.default_workspace.workspace_id
        empty = client.get(f"/api/workspaces/{workspace_id}/capabilities")
        assert empty.status_code == 200
        body = empty.json()
        assert body["workspace_id"] == workspace_id
        assert body["skills"] == []
        assert body["mcp"] == []
        assert body["notes"] == ["未配置 mcp.json"]
        assert body["paths"]["skills_dir"].endswith("skills")
        assert client.get("/api/workspaces/ws_missing/capabilities").status_code == 404

        (isolated_workspace / "mcp.json").write_text("{not json", encoding="utf-8")
        broken = client.get(f"/api/workspaces/{workspace_id}/capabilities")
        assert broken.status_code == 200
        body = broken.json()
        assert body["mcp"] == []
        assert len(body["notes"]) == 1
        assert "解析失败" in body["notes"][0]


def test_api_workspace_capabilities_configured_reports_no_secrets(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    workspace_id = runtime.default_workspace.workspace_id
    (isolated_workspace / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "docs": {
                        "command": "uvx",
                        "args": ["docs-mcp"],
                        "env": ["DOCS_TOKEN"],
                    },
                    "web": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example/s",
                        "headers": {"Authorization": "Bearer top-secret"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    skill_dir = isolated_workspace / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review code for problems\n---\n# Steps",
        encoding="utf-8",
    )
    with TestClient(create_app(runtime)) as client:
        response = client.get(f"/api/workspaces/{workspace_id}/capabilities")
        assert response.status_code == 200
        body = response.json()
        assert body["skills"] == [
            {"name": "code-review", "description": "Review code for problems"}
        ]
        assert body["mcp"] == [
            {"name": "docs", "transport": "stdio"},
            {"name": "web", "transport": "streamable-http"},
        ]
        assert body["paths"]["mcp_config"].endswith("mcp.json")
        # Commands, arguments, urls, env names, and headers never leak.
        for secret in ("uvx", "docs-mcp", "DOCS_TOKEN", "top-secret", "mcp.example"):
            assert secret not in response.text


def test_api_upload_attachment_success_and_rejections(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        created = client.post(
            f"/api/sessions/{session}/attachments",
            json={"name": "notes.py", "content_base64": encoded("print(1)")},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["attachment_id"].startswith("att_")
        assert body["name"] == "notes.py"
        assert body["size"] == len(b"print(1)")

        assert (
            client.post(
                "/api/sessions/missing-session/attachments",
                json={"name": "x.txt", "content_base64": ""},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/sessions/{session}/attachments",
                json={"name": "bad.txt", "content_base64": "not!!base64"},
            ).status_code
            == 422
        )
        binary = client.post(
            f"/api/sessions/{session}/attachments",
            json={
                "name": "bin.bin",
                "content_base64": base64.b64encode(b"\xff\xfe\x00bin").decode("ascii"),
                "media_type": "application/octet-stream",
            },
        )
        assert binary.status_code == 201
        assert binary.json()["kind"] == "file"
        assert (
            client.post(
                f"/api/sessions/{session}/attachments",
                json={"name": " ", "content_base64": ""},
            ).status_code
            == 422
        )

        listed = client.get(f"/api/sessions/{session}/attachments").json()
        assert {item["name"] for item in listed} == {"notes.py", "bin.bin"}
        binary_id = binary.json()["attachment_id"]
        downloaded = client.get(f"/api/sessions/{session}/attachments/{binary_id}")
        assert downloaded.content == b"\xff\xfe\x00bin"
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert client.delete(
            f"/api/sessions/{session}/attachments/{binary_id}"
        ).status_code == 200
        assert {item["name"] for item in client.get(
            f"/api/sessions/{session}/attachments"
        ).json()} == {"notes.py"}
        oversized = base64.b64encode(b"x" * (MAX_UPLOAD_BYTES + 1)).decode("ascii")
        assert (
            client.post(
                f"/api/sessions/{session}/attachments",
                json={"name": "big.txt", "content_base64": oversized},
            ).status_code
            == 422
        )


def test_api_run_with_attachments_inlines_and_truncates(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        small = client.post(
            f"/api/sessions/{session}/attachments",
            json={"name": "notes.py", "content_base64": encoded("print('hi')")},
        ).json()
        big = client.post(
            f"/api/sessions/{session}/attachments",
            json={"name": "big.log", "content_base64": encoded("你" * 300_000)},
        ).json()
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={
                "prompt": "summarize",
                "attachment_ids": [small["attachment_id"], big["attachment_id"]],
            },
        )
        assert accepted.status_code == 202
        run = wait_for_run(client, accepted.json()["run_id"])
        message = next(
            event
            for event in runtime.store.run_events(run["id"])
            if event["type"] == "message.user"
        )
        content = message["payload"]["message"]["content"]
        assert content.startswith("summarize")
        assert "--- 附件：notes.py ---" in content
        assert "print('hi')" in content
        assert "--- 附件：big.log ---" in content
        assert "[附件内容过长，已截断]" in content
        inlined = content.split("--- 附件：big.log ---\n", 1)[1].rsplit(
            "\n[附件内容过长，已截断]", 1
        )[0]
        assert len(inlined.encode("utf-8")) <= MAX_INLINE_BYTES


def test_api_image_attachment_is_hydrated_only_for_provider(isolated_workspace):
    provider = ScriptedProvider([{"text": "done"}])
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        image = client.post(
            f"/api/sessions/{session}/attachments",
            json={
                "name": "diagram.png",
                "media_type": "image/png",
                "content_base64": base64.b64encode(b"png-bytes").decode("ascii"),
            },
        ).json()
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "look", "attachment_ids": [image["attachment_id"]]},
        )
        assert accepted.status_code == 202
        run = wait_for_run(client, accepted.json()["run_id"])
        assert run["status"] == "completed"
        provider_block = provider.requests[0].messages[0]["content"][1]
        assert provider_block["type"] == "image"
        assert provider_block["data"] == base64.b64encode(b"png-bytes").decode("ascii")
        event = next(
            item
            for item in runtime.store.run_events(run["id"])
            if item["type"] == "message.user"
        )
        stored_block = event["payload"]["message"]["content"][1]
        assert stored_block["type"] == "attachment"
        assert "data" not in stored_block
        assert client.get(f"/api/sessions/{session}/attachments").json() == []
        history = client.get(
            f"/api/sessions/{session}/attachments?include_used=true"
        ).json()
        assert history[0]["used"] is True
        assert client.delete(
            f"/api/sessions/{session}/attachments/{image['attachment_id']}"
        ).status_code == 409


def test_api_run_rejects_unknown_and_foreign_attachments(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        other = client.post("/api/sessions").json()["session_id"]
        uploaded = client.post(
            f"/api/sessions/{session}/attachments",
            json={"name": "a.txt", "content_base64": encoded("hi")},
        ).json()
        missing = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "p", "attachment_ids": ["att_ffffffffffff"]},
        )
        assert missing.status_code == 422
        foreign = client.post(
            f"/api/sessions/{other}/runs",
            json={"prompt": "p", "attachment_ids": [uploaded["attachment_id"]]},
        )
        assert foreign.status_code == 422
        # A rejected attachment list must not leave a run behind.
        assert client.get(f"/api/sessions/{session}/runs").json() == []


def test_api_run_summary_includes_usage(isolated_workspace):
    runtime = AgentRuntime(
        settings_for(isolated_workspace),
        ScriptedProvider([{"text": "done", "usage": {"input_tokens": 11, "output_tokens": 7}}]),
    )
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "hello"})
        run = wait_for_run(client, accepted.json()["run_id"])
        expected = {"input_tokens": 11, "output_tokens": 7}
        listed = client.get(f"/api/sessions/{session}/runs").json()
        assert listed[-1]["usage"] == expected
        status = client.get(f"/api/sessions/{session}").json()
        assert status["latest_run"]["usage"] == expected
        assert run["usage"] == expected


def test_api_run_accepts_per_message_model(isolated_workspace):
    provider = ScriptedProvider([{"text": "done"}])
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "hello", "model": "one-off-model"},
        )
        assert accepted.status_code == 202
        wait_for_run(client, accepted.json()["run_id"])
        assert provider.requests[0].model == "one-off-model"


def test_api_run_plan_mode_blocks_write_tool(isolated_workspace):
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "w",
                        "name": "write_file",
                        "arguments": {"path": "evil.txt", "content": "nope"},
                    }
                ]
            },
            {"text": "planned"},
        ]
    )
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs", json={"prompt": "plan it", "mode": "plan"}
        )
        assert accepted.status_code == 202
        run = wait_for_run(client, accepted.json()["run_id"])
        assert run["status"] == "completed"
        catalog = {tool["name"] for tool in provider.requests[0].tools}
        assert not catalog & {"write_file", "edit_file", "bash"}
        assert not (isolated_workspace / "evil.txt").exists()


def test_api_run_agent_mode_auto_approves(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "d", "name": "bash", "arguments": {"command": "rm x"}}]},
            {"text": "agent done"},
        ]
    )
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs", json={"prompt": "clean", "mode": "agent"}
        )
        run = wait_for_run(client, accepted.json()["run_id"])
        assert run["status"] == "completed"
        assert run["output"] == "agent done"
        events = runtime.store.get_events(run["id"])
        resolved = [event for event in events if event["type"] == "approval.resolved"]
        assert len(resolved) == 1
        assert resolved[0]["payload"]["approved"] is True
        assert resolved[0]["payload"]["auto"] is True
        assert resolved[0]["author"] == "runtime"
        completed = next(event for event in events if event["type"] == "tool.result")
        assert not completed["payload"]["content"].startswith("Permission denied")
