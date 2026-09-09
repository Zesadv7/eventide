"""Tool executor policy and handler behavior tests."""

import sys

from eventide.executor import LocalCommandExecutor, ToolContext, ToolExecutor
from eventide.models import ToolCall
from eventide.policy import PolicyEngine


async def test_executor_handles_sync_async_unknown_and_errors(isolated_workspace):
    async def async_handler(value):
        return f"async:{value}"

    def sync_handler(value):
        return f"sync:{value}"

    def broken():
        raise RuntimeError("boom")

    executor = ToolExecutor(
        {"custom_sync": sync_handler, "custom_async": async_handler, "broken": broken}
    )
    context = ToolContext(isolated_workspace, "run", PolicyEngine())
    sync_result = await executor.execute(ToolCall("1", "custom_sync", {"value": 2}), context)
    async_result = await executor.execute(ToolCall("2", "custom_async", {"value": 3}), context)
    unknown = await executor.execute(ToolCall("3", "missing", {}), context)
    failed = await executor.execute(ToolCall("4", "broken", {}), context)
    assert sync_result.content == "sync:2"
    assert async_result.content == "async:3"
    assert unknown.is_error and "Unknown tool" in unknown.content
    assert failed.is_error and "boom" in failed.content


async def test_readonly_external_tool_skips_approval(isolated_workspace):
    executor = ToolExecutor({"mcp__demo__read": lambda: "ok"}, {"mcp__demo__read"})
    result = await executor.execute(
        ToolCall("1", "mcp__demo__read", {}),
        ToolContext(isolated_workspace, "run", PolicyEngine()),
    )
    assert result.content == "ok"


async def test_command_executor_is_replaceable(isolated_workspace):
    class FakeCommandExecutor:
        async def execute(self, command, cwd):
            return f"isolated:{command}:{cwd.name}"

    executor = ToolExecutor(
        {"bash": lambda command, cwd=None: "host"},
        command_executor=FakeCommandExecutor(),
    )
    result = await executor.execute(
        ToolCall("1", "bash", {"command": "echo ok"}),
        ToolContext(isolated_workspace, "run", PolicyEngine()),
    )
    assert result.content.startswith("isolated:echo ok")


async def test_local_command_timeout_terminates_process(isolated_workspace):
    executor = LocalCommandExecutor(timeout=0.05)
    output = await executor.execute(
        f'"{sys.executable}" -c "import time; time.sleep(30)"',
        isolated_workspace,
    )
    assert output == "Error: Timeout (0.05s)"


async def test_model_cannot_supply_its_own_mcp_approval(isolated_workspace):
    called = []
    executor = ToolExecutor({"mcp__demo__write": lambda **kw: called.append(kw)})
    result = await executor.execute(
        ToolCall("1", "mcp__demo__write", {"_eventide_approved": True}),
        ToolContext(isolated_workspace, "run", PolicyEngine()),
    )
    assert result.is_error and not called
