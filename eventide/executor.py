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

from eventide.models import CommandResult, ToolCall, ToolResult
from eventide.policy import PolicyDecision, PolicyEngine
from eventide.utils import decode_output

ApprovalHandler = Callable[[str, ToolCall, str], Awaitable[bool]]


class CommandExecutor(Protocol):
    """Replaceable boundary for local or future isolated command execution."""

    async def execute(self, command: str, cwd: Path) -> CommandResult | str: ...


class _BoundedOutput:
    """Retain the beginning and end while draining every output byte."""

    def __init__(self, limit: int):
        self.head_limit = (limit + 1) // 2
        self.tail_limit = limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total_bytes = 0

    def append(self, data: bytes) -> None:
        self.total_bytes += len(data)
        head_size = min(self.head_limit - len(self.head), len(data))
        self.head.extend(data[:head_size])
        if self.tail_limit:
            self.tail.extend(data[head_size:])
            del self.tail[: max(0, len(self.tail) - self.tail_limit)]

    @property
    def omitted_bytes(self) -> int:
        return self.total_bytes - len(self.head) - len(self.tail)

    @staticmethod
    def _decode_fragment(raw: bytearray, *, tail: bool = False) -> str:
        data = bytes(raw)
        candidate = data
        if tail:
            # A retained tail can start inside a UTF-8 character.
            skipped = 0
            while skipped < min(3, len(data)) and 0x80 <= data[skipped] < 0xC0:
                skipped += 1
            candidate = data[skipped:]
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(candidate):
                return decode_output(data)
        # An incomplete UTF-8 boundary must not cause the whole fragment to be
        # misdetected as the Windows console code page.
        return data.decode("utf-8", errors="replace")

    def render(self) -> str:
        if not self.omitted_bytes:
            return decode_output(bytes(self.head + self.tail))
        return (
            self._decode_fragment(self.head)
            + f"\n[... {self.omitted_bytes} output bytes omitted ...]\n"
            + self._decode_fragment(self.tail, tail=True)
        )


class LocalCommandExecutor:
    """Run commands on the host; this is explicitly not a security sandbox."""

    def __init__(self, timeout: float = 120.0, output_limit: int = 25_000):
        if timeout <= 0 or output_limit < 2:
            raise ValueError("Command timeout must be positive and output_limit at least 2 bytes")
        self.timeout = timeout
        self.output_limit = output_limit

    async def execute(self, command: str, cwd: Path) -> CommandResult:
        started = time.monotonic()
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        stdout = _BoundedOutput(self.output_limit)
        stderr = _BoundedOutput(self.output_limit)
        collection = asyncio.create_task(self._collect(process, stdout, stderr))
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(collection), self.timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate(process, collection)
        except asyncio.CancelledError:
            await self._terminate(process, collection)
            raise
        return CommandResult(
            exit_code=process.returncode,
            stdout=stdout.render(),
            stderr=stderr.render(),
            timed_out=timed_out,
            duration_ms=(time.monotonic() - started) * 1000,
            stdout_bytes=stdout.total_bytes,
            stderr_bytes=stderr.total_bytes,
            stdout_omitted_bytes=stdout.omitted_bytes,
            stderr_omitted_bytes=stderr.omitted_bytes,
        )

    @staticmethod
    async def _collect(
        process: asyncio.subprocess.Process, stdout: _BoundedOutput, stderr: _BoundedOutput
    ) -> None:
        async def drain(stream: asyncio.StreamReader | None, output: _BoundedOutput) -> None:
            if stream is not None:
                while data := await stream.read(8192):
                    output.append(data)

        await asyncio.gather(
            drain(process.stdout, stdout), drain(process.stderr, stderr), process.wait()
        )

    @staticmethod
    async def _signal_process(process: asyncio.subprocess.Process, *, force: bool) -> None:
        if os.name == "nt":
            try:
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
                if killer.returncode and process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
            except OSError:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
        else:
            with suppress(ProcessLookupError):
                # These POSIX-only names are absent from Windows type stubs.
                sig = getattr(signal, "SIGKILL") if force else signal.SIGTERM  # noqa: B009
                getattr(os, "killpg")(process.pid, sig)  # noqa: B009

    @classmethod
    async def _terminate(
        cls, process: asyncio.subprocess.Process, collection: asyncio.Task[None]
    ) -> None:
        cleanup = asyncio.create_task(cls._cleanup(process, collection))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Repeated stop requests must not interrupt process cleanup.
                cancelled = True
        await cleanup
        if cancelled:
            raise asyncio.CancelledError

    @classmethod
    async def _cleanup(
        cls, process: asyncio.subprocess.Process, collection: asyncio.Task[None]
    ) -> None:
        # Signal the group even if the shell exited: descendants may still own
        # its output pipes. Keep draining while waiting so termination cannot
        # deadlock on a full pipe.
        await cls._signal_process(process, force=False)
        try:
            await asyncio.wait_for(asyncio.shield(collection), 2)
        except asyncio.TimeoutError:
            await cls._signal_process(process, force=True)
            try:
                await asyncio.wait_for(asyncio.shield(collection), 2)
            except asyncio.TimeoutError:
                collection.cancel()
                with suppress(asyncio.CancelledError):
                    await collection


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
            if isinstance(output, CommandResult):
                return ToolResult(
                    call.id, call.name, output.render(), output.is_error, output.metadata()
                )
            text = str(output)
            return ToolResult(call.id, call.name, text, text.startswith("Error:"))
        except Exception as exc:
            return ToolResult(call.id, call.name, f"Error: {type(exc).__name__}: {exc}", True)
