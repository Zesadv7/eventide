"""MCP outcomes retain explicit failures and never retry an uncertain remote effect."""

import asyncio
from types import SimpleNamespace

import pytest
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from httpx2 import ReadError
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, INVALID_PARAMS, REQUEST_TIMEOUT

from eventide.executor import ToolContext, ToolExecutor
from eventide.host import RuntimeHost
from eventide.mcp.client import _tool_handler
from eventide.models import RunRequest, ToolCall
from eventide.policy import PolicyEngine
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for

NAME = "mcp__demo__write"
TOOLS = [{"name": NAME, "input_schema": {
    "type": "object", "properties": {}, "additionalProperties": False,
}}]


async def approve(*args):
    return True


async def test_transport_loss_is_unknown_after_remote_effect(isolated_workspace):
    effects = []

    class Session:
        async def call_tool(self, name, arguments):
            effects.append(name)
            raise ConnectionError("response lost after write")

    executor = ToolExecutor({NAME: _tool_handler(Session(), "write", None)}, tools=TOOLS)
    result = await executor.execute(
        ToolCall("call", NAME, {}),
        ToolContext(isolated_workspace, "run", PolicyEngine(), approval_handler=approve),
    )
    assert effects == ["write"]
    assert result.metadata.get("outcome_unknown") is True


async def test_successful_error_prefixed_mcp_text_is_not_failure(isolated_workspace):
    class Session:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(
                content=[SimpleNamespace(text="Error: sample source text")], isError=False,
            )

    executor = ToolExecutor({NAME: _tool_handler(Session(), "write", None)}, tools=TOOLS)
    result = await executor.execute(
        ToolCall("call", NAME, {}),
        ToolContext(isolated_workspace, "run", PolicyEngine(), approval_handler=approve),
    )
    assert result.content == "Error: sample source text"
    assert not result.is_error


@pytest.mark.parametrize("failure", [
    TimeoutError("transport timeout"), ConnectionError("disconnected"),
    BrokenResourceError(), ClosedResourceError(), EndOfStream(), ReadError("response lost"),
    MCPError(code=CONNECTION_CLOSED, message="closed"),
    MCPError(code=REQUEST_TIMEOUT, message="timeout"),
])
async def test_transport_failures_park_host_and_survive_restart(isolated_workspace, failure):
    repo = make_repo(isolated_workspace / "repo")
    effects = []

    class Session:
        async def call_tool(self, name, arguments):
            effects.append(name)
            raise failure

    provider = ScriptedProvider([{"tool_calls": [
        {"id": f"call-{i}", "name": NAME, "arguments": {}} for i in (1, 2)
    ]}, {"text": "unsafe"}])
    settings = settings_for(repo)
    host = RuntimeHost(settings, provider, tools=TOOLS,
                       handlers={NAME: _tool_handler(Session(), "write", None)})
    try:
        result = await host.run(RunRequest("write", mode="agent"))
        assert result.status == "interrupted"
        assert host.store.get_run(result.run_id)["reason"] == "unknown_tool_outcome"
        unknown = [e for e in host.store.run_events(result.run_id)
                   if e["type"] == "tool.outcome_unknown"]
        assert len(unknown) == 1 and unknown[0]["payload"]["call_id"] == "call-1"
        assert "MCPOutcomeUnknown" in unknown[0]["payload"]["error"]
        assert effects == ["write"] and len(provider.requests) == 1
        with pytest.raises(
            ValueError, match="Workspace has an unresolved tool outcome; execution is blocked"
        ):
            await host.continue_session(result.session_id)
    finally:
        await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    try:
        assert reopened.store.get_run(result.run_id)["reason"] == "unknown_tool_outcome"
        with pytest.raises(
            ValueError, match="Workspace has an unresolved tool outcome; execution is blocked"
        ):
            await reopened.continue_session(result.session_id)
        assert effects == ["write"]
    finally:
        await reopened.close()


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
async def test_inflight_timeout_and_cancel_keep_remote_outcome_unknown(isolated_workspace, stop):
    repo = make_repo(isolated_workspace / "repo")
    entered = asyncio.Event()
    effects = []

    class Session:
        async def call_tool(self, name, arguments):
            effects.append(name)
            entered.set()
            await asyncio.Future()

    provider = ScriptedProvider([{"tool_calls": [
        {"id": "write", "name": NAME, "arguments": {}}
    ]}])
    host = RuntimeHost(settings_for(repo), provider, tools=TOOLS, handlers={
        NAME: _tool_handler(Session(), "write", 0.02 if stop == "deadline" else None)
    })
    session_id = host.create_session()
    task = asyncio.create_task(host.run(RunRequest("write", session_id, mode="agent")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        else:
            assert (await asyncio.wait_for(task, 5)).status == "interrupted"
        state = host.store.latest_run_summary(session_id)
        assert state["reason"] == "unknown_tool_outcome"
        assert effects == ["write"] and len(provider.requests) == 1
        with pytest.raises(
            ValueError, match="Workspace has an unresolved tool outcome; execution is blocked"
        ):
            await host.continue_session(session_id)
    finally:
        await host.close()


async def test_explicit_mcp_errors_enter_existing_three_failure_window(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    effects = []

    class Session:
        async def call_tool(self, name, arguments):
            effects.append(name)
            return SimpleNamespace(content=[SimpleNamespace(text="cannot complete")], isError=True)

    provider = ScriptedProvider([{"tool_calls": [
        {"id": f"call-{i}", "name": NAME, "arguments": {}} for i in range(4)
    ]}])
    host = RuntimeHost(settings_for(repo), provider, tools=TOOLS,
                       handlers={NAME: _tool_handler(Session(), "write", None)})
    try:
        result = await host.run(RunRequest("write", mode="agent"))
        assert result.status == "interrupted"
        assert host.store.get_run(result.run_id)["reason"] == "repeated_tool_failure"
        completed = [e["payload"] for e in host.store.run_events(result.run_id)
                     if e["type"] == "tool.completed"]
        assert len(completed) == len(effects) == 3
        assert all(e["is_error"] and e["metadata"]["mcp_is_error"] for e in completed)
        assert all(e["content"] == "cannot complete" for e in completed)
    finally:
        await host.close()


@pytest.mark.parametrize("blocked", ["permission", "arguments", "protocol"])
async def test_rejection_is_not_transport_unknown(isolated_workspace, blocked):
    effects = []

    class Session:
        async def call_tool(self, name, arguments):
            effects.append(name)
            raise MCPError(code=INVALID_PARAMS, message="invalid tool parameters")

    executor = ToolExecutor({NAME: _tool_handler(Session(), "write", None)}, tools=TOOLS)
    result = await executor.execute(
        ToolCall("call", NAME, {"invalid": True} if blocked == "arguments" else {}),
        ToolContext(isolated_workspace, "run", PolicyEngine(),
                    approval_handler=None if blocked == "permission" else approve),
    )
    assert result.is_error and not result.metadata.get("outcome_unknown")
    assert effects == (["write"] if blocked == "protocol" else [])
    assert result.metadata["code"] == {
        "permission": "permission_denied", "arguments": "invalid_arguments",
        "protocol": "execution_failed",
    }[blocked]
