"""Async tool execution with a single policy boundary."""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from eventide.models import ToolCall, ToolResult
from eventide.policy import PolicyDecision, PolicyEngine
from eventide.utils import decode_output

ApprovalHandler = Callable[[str, ToolCall, str], Awaitable[bool]]


class CommandExecutor(Protocol):
    """Replaceable boundary for local or future isolated command execution."""

    async def execute(self, command: str, cwd: Path) -> str: ...


class LocalCommandExecutor:
    """Run commands on the host; this is explicitly not a security sandbox."""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout

    async def execute(self, command: str, cwd: Path) -> str:
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            await self._terminate(process)
            return f"Error: Timeout ({self.timeout:g}s)"
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        output = (decode_output(stdout) + decode_output(stderr)).strip()
        return output[:50_000] if output else "(no output)"

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            if os.name != "nt":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
            else:
                process.kill()
            await process.wait()


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
