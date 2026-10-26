"""Async tool execution with a single policy boundary."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from eventide.budgets import BudgetExceeded
from eventide.command_logs import (
    CommandLogCapture,
    CommandLogs,
    activate_capture,
    deactivate_capture,
)
from eventide.file_operations import FileOperations
from eventide.models import CommandResult, ToolCall, ToolOutput, ToolResult
from eventide.policy import PolicyDecision, PolicyEngine
from eventide.tool_contracts import (
    BUILTIN_ARGUMENTS,
    ToolArgumentsError,
    ToolContractError,
    builtin_schema,
    contract_catalog,
)
from eventide.tools.filesystem import run_edit, run_write
from eventide.utils import decode_output

CURRENT_TOOL_CALL: ContextVar[ToolCall] = ContextVar("eventide_tool_call")

ApprovalHandler = Callable[[str, ToolCall, str], Awaitable[bool]]


class CommandExecutor(Protocol):
    """Replaceable boundary for local or future isolated command execution."""

    async def execute(self, command: str, cwd: Path) -> CommandResult | str: ...


class CoordinatedCommandExecutor:
    """Serialize commands that may mutate Git metadata shared by linked checkouts."""

    def __init__(self, delegate: CommandExecutor, lock: asyncio.Lock):
        self.delegate = delegate
        self.lock = lock

    @property
    def backend_kind(self) -> str:
        return str(getattr(self.delegate, "backend_kind", "custom"))

    async def execute(self, command: str, cwd: Path) -> CommandResult | str:
        async with self.lock:
            return await self.delegate.execute(command, cwd)

    async def execute_argv(
        self, arguments: list[str], cwd: Path, *, extra_env: dict[str, str] | None = None,
    ) -> CommandResult | str:
        execute = getattr(self.delegate, "execute_argv", None)
        if not callable(execute):
            raise ValueError("Command backend does not support direct argument execution")
        async with self.lock:
            result: CommandResult | str = await execute(arguments, cwd, extra_env=extra_env)
            return result


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

    backend_kind = "local"

    def __init__(
        self,
        timeout: float = 120.0,
        output_limit: int = 25_000,
        *,
        output_observer: Callable[[str, bytes], None] | None = None,
    ):
        if timeout <= 0 or output_limit < 2:
            raise ValueError("Command timeout must be positive and output_limit at least 2 bytes")
        self.timeout = timeout
        self.output_limit = output_limit
        self.output_observer = output_observer

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
        return await self._finish_process(process, started)

    async def execute_argv(
        self, arguments: list[str], cwd: Path, *, extra_env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run a packaged script without passing its arguments through a shell."""
        started = time.monotonic()
        options: dict[str, Any] = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt" else {"start_new_session": True}
        )
        process = await asyncio.create_subprocess_exec(
            *arguments, cwd=cwd, env={**os.environ, **(extra_env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
        )
        return await self._finish_process(process, started)

    async def _finish_process(
        self, process: asyncio.subprocess.Process, started: float,
    ) -> CommandResult:
        stdout = _BoundedOutput(self.output_limit)
        stderr = _BoundedOutput(self.output_limit)
        collection = asyncio.create_task(
            self._collect(process, stdout, stderr, self.output_observer)
        )
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
        process: asyncio.subprocess.Process,
        stdout: _BoundedOutput,
        stderr: _BoundedOutput,
        output_observer: Callable[[str, bytes], None] | None = None,
    ) -> None:
        async def drain(
            stream: asyncio.StreamReader | None, output: _BoundedOutput, name: str,
        ) -> None:
            if stream is not None:
                while data := await stream.read(8192):
                    output.append(data)
                    if output_observer is not None:
                        output_observer(name, data)

        await asyncio.gather(
            drain(process.stdout, stdout, "stdout"),
            drain(process.stderr, stderr, "stderr"),
            process.wait(),
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
        *,
        tools: list[dict[str, Any]] | None = None,
        file_operations: FileOperations | None = None,
        workspace_id: str | None = None,
        command_logs: CommandLogs | None = None,
    ):
        self.handlers = dict(handlers)
        self.file_operations = file_operations
        self.workspace_id = workspace_id
        self.command_logs = command_logs
        self.readonly_tools = set(readonly_tools or ())
        self.command_executor = command_executor or LocalCommandExecutor()
        self.contracts = contract_catalog(
            tools
            if tools is not None
            else [
                {"name": name, "input_schema": builtin_schema(name)}
                for name in handlers
                if name in BUILTIN_ARGUMENTS
            ]
        )

    def register_discovered_tool(
        self,
        name: str,
        input_schema: object,
        handler: Callable[..., object],
        *,
        readonly: bool = False,
    ) -> None:
        """Unlock one catalog-frozen tool after an explicit discovery load (MK-16).

        The schema comes from the run's frozen catalog snapshot, so this cannot
        swap an implementation under a same name; an invalid schema stays a
        ToolContractError and the call is rejected fail-closed.
        """
        contract = contract_catalog([{"name": name, "input_schema": input_schema}])[name]
        self.contracts[name] = contract
        self.handlers[name] = handler
        if readonly:
            self.readonly_tools.add(name)

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        capture, start_error = self._start_command_log(call, context)
        token = activate_capture(capture) if capture is not None else None
        call_token = CURRENT_TOOL_CALL.set(call)
        try:
            try:
                result = await self._dispatch(call, context)
            except BaseException:
                # Cancel or abort still seals what was captured before the cut.
                if capture is not None:
                    self._settle_command_log(capture, None, "cancelled")
                raise
            ran = result.metadata.get("exit_code") is not None or (
                result.metadata.get("outcome_unknown")
            )
            self._settle_command_log(
                capture, result, "complete" if ran else "not_started",
            )
            if start_error is not None:
                result.metadata.setdefault(
                    "command_log",
                    {"available": False, "availability": "start_failed",
                     "run_id": context.run_id, "call_id": call.id, "streams": {},
                     "detail": start_error},
                )
            if capture is not None and (
                result.metadata.get("stdout_omitted_bytes")
                or result.metadata.get("stderr_omitted_bytes")
            ):
                result = replace(result, content=result.content + (
                    "\n[Recorded command streams may contain omitted output. "
                    f'Use read_tool_result run_id="{capture.run_id}", '
                    f'call_id="{capture.call_id}", stream="stdout" or "stderr", '
                    "offset and limit. Availability and capture limits are reported.]"
                ))
            return result
        finally:
            CURRENT_TOOL_CALL.reset(call_token)
            deactivate_capture(token)

    def _start_command_log(
        self, call: ToolCall, context: ToolContext,
    ) -> tuple[CommandLogCapture | None, str | None]:
        """Bind this exact call's full streams to a redacted disk log (MK05)."""
        if self.command_logs is None or call.name not in {"bash", "run_skill_script"}:
            return None, None
        try:
            capture = self.command_logs.start(
                context.run_id, call.id,
                secrets=self.command_logs.secrets, clean=self.command_logs.clean,
            )
        except (OSError, sqlite3.Error) as exc:
            # Storage errors do not change the actual command result.
            return None, type(exc).__name__
        return capture, None

    def _settle_command_log(
        self, capture: CommandLogCapture | None, result: ToolResult | None, outcome: str,
    ) -> None:
        if capture is None:
            return
        try:
            reference = capture.finish(outcome)
        except (OSError, sqlite3.Error) as exc:
            logging.getLogger(__name__).error(
                "Command log receipt failed: run=%s call=%s error=%s",
                capture.run_id, capture.call_id, type(exc).__name__,
            )
            reference = {
                "artifact_id": capture.artifact, "run_id": capture.run_id,
                "call_id": capture.call_id, "streams": {},
                "available": False, "availability": "record_failed",
                "detail": type(exc).__name__,
            }
        if result is not None:
            result.metadata["command_log"] = reference


    async def _dispatch(self, call: ToolCall, context: ToolContext) -> ToolResult:
        handler = self.handlers.get(call.name)
        if handler is None:
            return ToolResult(
                call.id, call.name, f"Unknown tool: {call.name}", True, {"code": "unknown_tool"}
            )
        contract = self.contracts.get(call.name)
        if contract is None or isinstance(contract, ToolContractError):
            reason = str(contract) if contract is not None else "No input contract was registered"
            return ToolResult(
                call.id,
                call.name,
                f"Error: invalid tool contract: {reason}",
                True,
                {"code": "invalid_tool_contract"},
            )
        try:
            arguments = contract.validate(call.arguments)
        except (ToolArgumentsError, ToolContractError) as exc:
            return ToolResult(
                call.id,
                call.name,
                f"Error: {exc}",
                True,
                {
                    "code": (
                        "invalid_arguments"
                        if isinstance(exc, ToolArgumentsError)
                        else "invalid_tool_contract"
                    )
                },
            )
        policy = context.policy.evaluate(
            call.name,
            arguments,
            context.cwd,
            trusted_readonly=call.name in self.readonly_tools,
        )
        if policy.decision == PolicyDecision.DENY:
            return ToolResult(
                call.id, call.name, f"Permission denied: {policy.reason}", True,
                {"code": "permission_denied"},
            )
        if policy.decision == PolicyDecision.ASK:
            if not context.approval_handler:
                return ToolResult(
                    call.id, call.name, f"Permission denied: {policy.reason}", True,
                    {"code": "permission_denied"},
                )
            approval_id = f"approval_{call.id}_{int(time.time() * 1000)}"
            validated_call = ToolCall(call.id, call.name, arguments)
            if not await context.approval_handler(approval_id, validated_call, policy.reason):
                return ToolResult(
                    call.id, call.name, "Permission denied by user or approval timeout", True,
                    {"code": "permission_denied"},
                )
        if call.name in {"bash", "read_file", "write_file", "edit_file", "glob", "search_text"}:
            arguments["cwd"] = context.cwd
        try:
            output: object
            if call.name == "bash":
                output = await self.command_executor.execute(
                    arguments["command"], context.cwd
                )
            elif self.file_operations is not None and (
                (call.name == "write_file" and handler is run_write)
                or (call.name == "edit_file" and handler is run_edit)
            ):
                assert self.workspace_id is not None
                output = await asyncio.to_thread(
                    self.file_operations.write, call.name, arguments,
                    run_id=context.run_id, call_id=call.id,
                    workspace_id=self.workspace_id, cwd=context.cwd,
                )
            elif inspect.iscoroutinefunction(handler):
                output = await handler(**arguments)
            else:
                output = await asyncio.to_thread(handler, **arguments)
            if isinstance(output, CommandResult):
                metadata = output.metadata()
                if output.is_error:
                    metadata["code"] = "timeout" if output.timed_out else "execution_failed"
                return ToolResult(
                    call.id, call.name, output.render(), output.is_error, metadata
                )
            if isinstance(output, ToolOutput):
                return ToolResult(
                    call.id, call.name, output.content, output.is_error, output.metadata
                )
            text = str(output)
            failed = text.startswith("Error:")
            return ToolResult(
                call.id, call.name, text, failed,
                {"code": "execution_failed"} if failed else {},
            )
        except BudgetExceeded as exc:
            return ToolResult(call.id, call.name, str(exc), True,
                              {"code": "budget_exceeded", "violations": exc.violations})
        except Exception as exc:
            metadata = {
                "code": "timeout" if isinstance(exc, TimeoutError) else "execution_failed",
                "exception_type": type(exc).__name__,
            }
            if getattr(exc, "outcome_unknown", False):
                metadata.update({"outcome_unknown": True, "code": "outcome_unknown"})
            return ToolResult(
                call.id, call.name, f"Error: {type(exc).__name__}: {exc}", True, metadata
            )
