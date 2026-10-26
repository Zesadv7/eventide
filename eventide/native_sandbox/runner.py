"""Host-side launcher and JSON-line client for the native sandbox worker.

The runner is the only component in this slice that spawns processes. It never
falls back to unsandboxed execution: a missing backend, an unsupported policy or
a failed launch raises an explicit error for the caller (the future file-tool
wiring inside ToolExecutor) to surface. The ``launcher`` constructor parameter
is a test-only injection point so offline tests can run the real worker
protocol without the OS wrapper; production callers must keep the default.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from eventide.native_sandbox.bubblewrap import build_bubblewrap_launch
from eventide.native_sandbox.policy import (
    NativeBackendKind,
    NativeSandboxPolicy,
    check_backend_support,
)
from eventide.native_sandbox.probe import BackendDetection, detect_backend, preferred_backend
from eventide.native_sandbox.seatbelt import build_seatbelt_launch, build_seatbelt_profile
from eventide.native_sandbox.worker import (
    GRANT_ENV_VAR,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    WORKER_BOOTSTRAP,
)

HANDSHAKE_TIMEOUT_SECONDS = 15.0
SHUTDOWN_GRACE_SECONDS = 5.0
STDERR_TAIL_BYTES = 8 * 1024


class NativeSandboxError(RuntimeError):
    """Base class for native sandbox failures."""


class NativeSandboxUnavailableError(NativeSandboxError):
    """The platform or the sandbox executable is unavailable; never a fallback."""


class WorkerLaunchError(NativeSandboxError):
    """The worker did not complete its handshake."""


class WorkerCrashedError(NativeSandboxError):
    """The worker process exited before responding."""


class WorkerProtocolError(NativeSandboxError):
    """Protocol framing violated; the runner stops being usable."""


class WorkerCallCancelledError(asyncio.CancelledError):
    """Cancellation retains whether a request crossed the worker transport."""

    def __init__(self, operation: str, dispatched: bool) -> None:
        super().__init__(f"Native worker {operation} cancelled; dispatched={dispatched}")
        self.dispatched = dispatched


class WorkerOperationError(NativeSandboxError):
    """The worker refused or failed the requested operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InterpreterInfo:
    executable: Path
    base_prefix: Path
    prefix: Path
    package_parent: Path

    @classmethod
    def resolve(cls) -> InterpreterInfo:
        package_parent = Path(__file__).resolve().parents[2]
        return cls(
            executable=Path(sys.executable).resolve(),
            base_prefix=Path(sys.base_prefix).resolve(),
            prefix=Path(sys.prefix).resolve(),
            package_parent=package_parent,
        )


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    env: dict[str, str]


Launcher = Callable[[NativeSandboxPolicy, InterpreterInfo], LaunchSpec]


def build_launch(
    policy: NativeSandboxPolicy,
    kind: NativeBackendKind,
    detection: BackendDetection,
    interpreter: InterpreterInfo,
    *,
    profile_path: Path | None = None,
) -> LaunchSpec:
    grant = policy.grant().encode_env()
    env = {"TMPDIR": str(policy.tmp_root), GRANT_ENV_VAR: grant}
    if kind is NativeBackendKind.SEATBELT:
        if profile_path is None:
            raise ValueError("Seatbelt launch requires a written profile path")
        argv = build_seatbelt_launch(
            policy,
            sandbox_exec=detection.binary_path or "sandbox-exec",
            interpreter=interpreter.executable,
            profile_path=profile_path,
            package_parent=interpreter.package_parent,
        )
    else:
        argv = build_bubblewrap_launch(
            policy,
            bwrap=detection.binary_path or "bwrap",
            interpreter=interpreter.executable,
            base_prefix=interpreter.base_prefix,
            prefix=interpreter.prefix,
            package_parent=interpreter.package_parent,
        )
    return LaunchSpec(argv=tuple(argv), env=env)


def _unsandboxed_test_launcher(
    policy: NativeSandboxPolicy, interpreter: InterpreterInfo
) -> LaunchSpec:
    """Launch the real worker without the OS wrapper; offline tests only.

    Production callers must never construct this: the sandbox wrapper is the
    isolation boundary and is not optional.
    """
    return LaunchSpec(
        argv=(
            str(interpreter.executable),
            "-I",
            "-c",
            WORKER_BOOTSTRAP,
            str(interpreter.package_parent),
        ),
        env={
            "TMPDIR": str(policy.tmp_root),
            GRANT_ENV_VAR: policy.grant().encode_env(),
        },
    )


class NativeSandboxRunner:
    """Spawn the isolated worker and exchange bounded JSON-line requests."""

    def __init__(
        self,
        policy: NativeSandboxPolicy,
        *,
        backend: NativeBackendKind | None = None,
        launcher: Launcher | None = None,
        call_timeout: float = 30.0,
    ) -> None:
        self._policy = policy
        self._backend = backend
        self._launcher = launcher
        self._call_timeout = call_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[bytes] | None = None
        self._profile_path: Path | None = None
        self._broken: str | None = None
        self._closed = False

    @property
    def policy(self) -> NativeSandboxPolicy:
        return self._policy

    async def start(self) -> None:
        if self._process is not None or self._closed:
            raise WorkerProtocolError("Runner already started or closed")
        interpreter = InterpreterInfo.resolve()
        if self._launcher is None:
            selection = (
                (self._backend, detect_backend(self._backend))
                if self._backend
                else preferred_backend()
            )
            if selection is None:
                raise NativeSandboxUnavailableError(
                    f"No native sandbox backend applies to platform {sys.platform}"
                )
            kind, detection = selection
            if not detection.available:
                raise NativeSandboxUnavailableError(detection.detail)
            check_backend_support(self._policy, kind)
            self._policy.tmp_root.mkdir(parents=True, exist_ok=True)
            if kind is NativeBackendKind.SEATBELT:
                self._profile_path = self._write_seatbelt_profile()
            spec = build_launch(
                self._policy, kind, detection, interpreter, profile_path=self._profile_path
            )
        else:
            if self._backend is None:
                raise ValueError("launcher override requires an explicit backend kind")
            check_backend_support(self._policy, self._backend)
            self._policy.tmp_root.mkdir(parents=True, exist_ok=True)
            spec = self._launcher(self._policy, interpreter)
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *spec.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spec.env,
                limit=MAX_MESSAGE_BYTES + 1024,
            ))
        try:
            self._process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            while not spawn.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(spawn)
            self._process = spawn.result()
            await self.aclose()
            raise
        except OSError as error:
            raise NativeSandboxUnavailableError(
                f"Failed to spawn sandbox worker: {error}"
            ) from error
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            handshake = await self.call("ping")
        except NativeSandboxError as error:
            await self.aclose()
            tail = await self._stderr_tail()
            raise WorkerLaunchError(
                f"Worker handshake failed: {error}; stderr: {tail!r}"
            ) from error
        if handshake.get("protocol") != PROTOCOL_VERSION:
            await self.aclose()
            raise WorkerLaunchError(f"Unsupported worker protocol: {handshake!r}")
        if handshake.get("profile") != self._policy.profile.value:
            await self.aclose()
            raise WorkerLaunchError(f"Worker started with unexpected profile: {handshake!r}")

    async def call(self, operation: str, **params: object) -> dict:
        process = self._require_process()
        if self._broken:
            raise WorkerProtocolError(f"Runner is unusable: {self._broken}")
        identifier = uuid.uuid4().hex
        line = json.dumps(
            {"v": PROTOCOL_VERSION, "id": identifier, "op": operation, "params": params}
        )
        assert process.stdin is not None and process.stdout is not None
        dispatched = False
        try:
            try:
                process.stdin.write(line.encode("utf-8") + b"\n")
                dispatched = True
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                self._broken = "worker stdin closed"
                raise WorkerCrashedError("Worker stdin closed before request") from error
            try:
                raw = await asyncio.wait_for(process.stdout.readline(), self._call_timeout)
            except asyncio.TimeoutError as error:
                self._broken = f"timeout waiting for {operation}"
                raise WorkerProtocolError(f"Timed out waiting for {operation} response") from error
            except (ValueError, asyncio.LimitOverrunError) as error:
                self._broken = "response exceeded the stream limit"
                raise WorkerProtocolError("Worker response exceeded the stream limit") from error
            if not raw:
                self._broken = "worker exited"
                tail = await self._stderr_tail()
                raise WorkerCrashedError(f"Worker exited before responding; stderr: {tail!r}")
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._broken = "malformed response"
                raise WorkerProtocolError("Worker response is not valid JSON") from error
            if not isinstance(response, dict) or response.get("id") != identifier:
                self._broken = "response id mismatch"
                raise WorkerProtocolError("Worker response does not match the request id")
            if response.get("ok") is not True:
                error_payload = response.get("error")
                if not isinstance(error_payload, dict):
                    self._broken = "malformed error response"
                    raise WorkerProtocolError("Worker error response is malformed")
                raise WorkerOperationError(
                    str(error_payload.get("code")), str(error_payload.get("message"))
                )
            result = response.get("result")
            if not isinstance(result, dict):
                self._broken = "malformed result"
                raise WorkerProtocolError("Worker result is not an object")
            return result
        except asyncio.CancelledError as error:
            self._broken = f"cancelled while awaiting {operation}"
            raise WorkerCallCancelledError(operation, dispatched) from error

    async def aclose(self) -> None:
        if self._closed:
            return
        process = self._process
        if process is not None:
            graceful = (NativeSandboxError, asyncio.TimeoutError, asyncio.CancelledError)
            with contextlib.suppress(*graceful):
                await asyncio.wait_for(self.call("shutdown"), SHUTDOWN_GRACE_SECONDS)
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), SHUTDOWN_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        if self._profile_path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._profile_path)
            self._profile_path = None
        self._process = None
        self._closed = True

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise WorkerProtocolError("Runner has not been started")
        return self._process

    def _write_seatbelt_profile(self) -> Path:
        interpreter = InterpreterInfo.resolve()
        descriptor, name = tempfile.mkstemp(
            prefix="eventide-seatbelt-", suffix=".sb", dir=self._policy.tmp_root
        )
        path = Path(name)
        text = build_seatbelt_profile(
            self._policy, interpreter.executable,
            runtime_roots=(interpreter.base_prefix, interpreter.prefix),
            worker_path=interpreter.package_parent / "eventide/native_sandbox/worker.py",
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o600)
        return path

    async def _drain_stderr(self) -> bytes:
        process = self._process
        if process is None or process.stderr is None:
            return b""
        chunks: list[bytes] = []
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except (ConnectionResetError, OSError) as error:
            chunks.append(f"[stderr capture failed: {error}]".encode())
        return b"".join(chunks)[-STDERR_TAIL_BYTES:]

    async def _stderr_tail(self) -> bytes:
        if self._stderr_task is None:
            return b""
        try:
            return await asyncio.shield(self._stderr_task)
        except asyncio.CancelledError:
            return b""
