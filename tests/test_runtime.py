"""Tests for async runtime behavior and isolation."""

import asyncio
from dataclasses import replace

from nexus_agent.config import Settings
from nexus_agent.models import RunRequest
from nexus_agent.observability import TraceStore
from nexus_agent.providers import ScriptedProvider
from nexus_agent.runtime import AgentRuntime


def settings_for(path, **changes):
    settings = Settings(
        workdir=path,
        state_dir=path / ".nexus",
        provider="scripted",
        api_key=None,
        base_url=None,
        model="scripted",
        fallback_model=None,
    )
    return replace(settings, **changes)


async def test_runtime_direct_answer_and_trace(isolated_workspace):
    runtime = AgentRuntime(
        settings_for(isolated_workspace),
        ScriptedProvider([{"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}}]),
    )
    result = await runtime.run(RunRequest("hello"))
    assert result.status == "completed"
    assert result.output == "done"
    assert result.usage["input_tokens"] == 3
    assert runtime.store.get_events(result.run_id)[-1]["type"] == "run.completed"
    await runtime.close()


async def test_runtime_tool_and_permission_denial(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "r", "name": "read_file", "arguments": {"path": "../x"}}]},
            {"text": "handled"},
        ]
    )
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    result = await runtime.run(RunRequest("read outside"))
    events = runtime.store.get_events(result.run_id)
    assert result.output == "handled"
    assert any("Permission denied" in event["payload"].get("content", "") for event in events)
    await runtime.close()


async def test_runtime_approval_handler(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "d", "name": "bash", "arguments": {"command": "rm x"}}]},
            {"text": "approved path complete"},
        ]
    )
    called = []

    async def approve(approval_id, call, reason):
        called.append((approval_id, call.name, reason))
        return False

    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    result = await runtime.run(RunRequest("delete"), approval_handler=approve)
    assert result.status == "completed"
    assert called[0][1] == "bash"
    types = [event["type"] for event in runtime.store.get_events(result.run_id)]
    assert "approval.required" in types and "approval.resolved" in types
    await runtime.close()


async def test_runtime_retries_and_compacts(isolated_workspace):
    settings = settings_for(isolated_workspace, context_limit=80)
    provider = ScriptedProvider(
        [
            {"error": "429", "retryable": True},
            {"text": "recovered"},
        ]
    )
    runtime = AgentRuntime(settings, provider)
    session = runtime.create_session("long")
    runtime.store.append_message(session, {"role": "user", "content": "x" * 300})
    result = await runtime.run(RunRequest("continue", session_id=session))
    types = [event["type"] for event in runtime.store.get_events(result.run_id)]
    assert result.output == "recovered"
    assert "model.retry" in types
    assert "context.compacted" in types
    await runtime.close()


async def test_sessions_are_isolated_and_can_run_concurrently(isolated_workspace):
    class EchoProvider:
        async def complete(self, request):
            await asyncio.sleep(0.01)
            from nexus_agent.models import ModelResponse

            return ModelResponse(str(request.messages[-1]["content"]))

    store = TraceStore(isolated_workspace / "shared.db")
    runtime = AgentRuntime(settings_for(isolated_workspace), EchoProvider(), store=store)
    first, second = await asyncio.gather(
        runtime.run(RunRequest("alpha", session_id="a")),
        runtime.run(RunRequest("beta", session_id="b")),
    )
    assert first.output == "alpha" and second.output == "beta"
    assert store.load_messages("a")[0]["content"] == "alpha"
    assert store.load_messages("b")[0]["content"] == "beta"
    await runtime.close()
    store.close()
