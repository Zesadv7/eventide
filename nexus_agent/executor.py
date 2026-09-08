"""Async tool execution with a single policy boundary."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nexus_agent.models import ToolCall, ToolResult
from nexus_agent.policy import PolicyDecision, PolicyEngine
from nexus_agent.tools.bash import run_bash

ApprovalHandler = Callable[[str, ToolCall, str], Awaitable[bool]]


class CommandExecutor(Protocol):
    """Replaceable boundary for local or future isolated command execution."""

    async def execute(self, command: str, cwd: Path) -> str: ...


class LocalCommandExecutor:
    """Run commands on the host; this is explicitly not a security sandbox."""

    async def execute(self, command: str, cwd: Path) -> str:
        return await asyncio.to_thread(run_bash, command, cwd)


@dataclass(slots=True)
class ToolContext:
    cwd: Path
    run_id: str
    policy: PolicyEngine
    approval_handler: ApprovalHandler | None = None


class ToolExecutor:
    def __init__(
        self,
        handlers: dict[str, Callable[..., object]],
        readonly_tools: set[str] | None = None,
        command_executor: CommandExecutor | None = None,
    ):
        self.handlers = handlers
        self.readonly_tools = readonly_tools or set()
        self.command_executor = command_executor or LocalCommandExecutor()

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        policy = context.policy.evaluate(
            call.name,
            dict(call.arguments),
            context.cwd,
            trusted_readonly=call.name in self.readonly_tools,
        )
        if policy.decision == PolicyDecision.DENY:
            return ToolResult(call.id, call.name, f"Permission denied: {policy.reason}", True)
        if policy.decision == PolicyDecision.ASK:
            if not context.approval_handler:
                return ToolResult(call.id, call.name, f"Permission denied: {policy.reason}", True)
            approval_id = f"approval_{call.id}_{int(time.time() * 1000)}"
            if not await context.approval_handler(approval_id, call, policy.reason):
                return ToolResult(
                    call.id, call.name, "Permission denied by user or approval timeout", True
                )
        handler = self.handlers.get(call.name)
        if handler is None:
            return ToolResult(call.id, call.name, f"Unknown tool: {call.name}", True)
        arguments = dict(call.arguments)
        if call.name in {"bash", "read_file", "write_file", "edit_file", "glob"}:
            arguments["cwd"] = context.cwd
        try:
            output: object
            if call.name == "bash":
                output = await self.command_executor.execute(
                    str(arguments.get("command", "")), context.cwd
                )
            elif inspect.iscoroutinefunction(handler):
                output = await handler(**arguments)
            else:
                output = await asyncio.to_thread(handler, **arguments)
            text = str(output)
            return ToolResult(call.id, call.name, text, text.startswith("Error:"))
        except Exception as exc:
            return ToolResult(call.id, call.name, f"Error: {type(exc).__name__}: {exc}", True)
