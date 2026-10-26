"""Explicit native sandbox execution profile wired into the production Host.

This module is the MK-21 slice-2 integration boundary (ADR-124): it validates
the fail-closed ``native`` execution profile, builds a session-frozen
:class:`NativeSandboxPolicy` for a real execution copy and routes file tools
through the isolated worker via :class:`NativeSandboxRunner`.

Hard boundaries:

- The profile is off by default and never chosen implicitly; enabling it is an
  explicit user request validated at admission.
- Every failure is explicit: a missing backend, an unsupported policy, a worker
  crash or a refused path surfaces as a tool/session error. There is no
  fallback to host execution and no silent retry.
- The worker executes file operations only. Shell commands, Skill scripts,
  network access and text search are not implemented on this boundary and are
  excluded from the frozen tool catalog in native mode; a hallucinated call
  fails as an unknown tool instead of running on the host.
- Platform isolation itself is NOT yet verified on a real kernel (MK-22).
  Availability probes only report whether a backend binary exists; UI text
  must present native execution as experimental and unverified.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from eventide.models import ToolOutput
from eventide.native_sandbox.policy import (
    NativeBackendKind,
    NativeSandboxPolicy,
    NativeSandboxProfile,
    UnsupportedPolicyError,
    check_backend_support,
    default_protected_paths,
)
from eventide.native_sandbox.runner import (
    NativeSandboxError,
    NativeSandboxRunner,
    WorkerOperationError,
)
from eventide.tools.filesystem import render_read_file

NATIVE_PROFILE_KIND = "native"
NATIVE_PROFILES = ("read-only", "workspace-write")
NATIVE_BACKENDS = ("seatbelt", "bubblewrap")


class NativeExecutionError(RuntimeError):
    """An explicit native execution failure; never a reason to run on the host."""

    code: str = "execution_failed"


def validate_native_profile(value: object) -> dict[str, Any]:
    """Fail-closed validation of the frozen native execution config."""
    if not isinstance(value, dict):
        raise ValueError("Native execution profile must be an object")
    if set(value) != {"kind", "profile", "backend"}:
        raise ValueError("Native execution profile is incomplete or unsupported")
    if value["kind"] != NATIVE_PROFILE_KIND:
        raise ValueError("Native execution profile has an invalid kind")
    if value["profile"] not in NATIVE_PROFILES:
        raise ValueError("Native execution profile must be read-only or workspace-write")
    if value["backend"] is not None and value["backend"] not in NATIVE_BACKENDS:
        raise ValueError("Native execution backend must be seatbelt or bubblewrap")
    from eventide.native_sandbox.probe import detect_backend, preferred_backend

    if value["backend"] is None:
        selection = preferred_backend()
        if selection is None:
            raise ValueError("No native backend applies to this platform")
        backend, detection = selection
    else:
        backend = NativeBackendKind(value["backend"])
        detection = detect_backend(backend)
    if not detection.available:
        raise ValueError(f"Native backend is unavailable: {detection.detail}")
    return {**value, "backend": backend.value}


def build_native_policy(
    working_root: Path,
    *,
    profile: str,
    state_root: Path,
    backend: str | None = None,
    git_common_dir: Path | None = None,
) -> NativeSandboxPolicy:
    """Freeze the sandbox policy for one real execution copy.

    ``working_root`` is the admitted execution copy (worktree). Runtime state
    stays protected; managed worktrees live under the state root, so the
    shared Git metadata is covered by the state-root protection.
    """
    policy = NativeSandboxPolicy(
        profile=NativeSandboxProfile(profile),
        workspace_root=working_root,
        tmp_root=Path(tempfile.mkdtemp(prefix="eventide-native-tmp-")),
        protected_roots=default_protected_paths(
            working_root, state_root=state_root, git_common_dir=git_common_dir
        ),
    )
    if backend is not None:
        check_backend_support(policy, NativeBackendKind(backend))
    return policy


class NativeWorker(Protocol):
    """The subset of :class:`NativeSandboxRunner` the file tools rely on."""

    async def start(self) -> None: ...

    async def call(self, operation: str, **params: object) -> dict: ...

    async def aclose(self) -> None: ...


class NativeFileTools:
    """Async file tool handlers executed inside the native sandbox worker.

    ``runner_factory`` defaults to the production sandboxed launcher. Tests may
    inject a factory returning a runner whose launcher runs the real worker
    without the OS wrapper, or an in-process fake implementing the same
    protocol; production wiring must keep the default.
    """

    def __init__(
        self,
        policy: NativeSandboxPolicy,
        *,
        backend: str | None = None,
        runner_factory: Callable[[NativeSandboxPolicy], NativeWorker] | None = None,
    ):
        self._policy = policy
        self._runner_factory = runner_factory or (
            lambda policy: NativeSandboxRunner(
                policy, backend=NativeBackendKind(backend) if backend else None,
            )
        )
        self._runner: NativeWorker | None = None
        self._start_lock = asyncio.Lock()
        self._closed = False

    @property
    def policy(self) -> NativeSandboxPolicy:
        return self._policy

    @property
    def handlers(self) -> dict[str, Callable[..., object]]:
        return {
            "read_file": self.read_file,
            "glob": self.glob,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
        }

    async def ensure_started(self) -> NativeWorker:
        if self._closed:
            raise NativeExecutionError("Native file tools are already closed")
        if self._runner is None:
            async with self._start_lock:
                if self._runner is None:
                    runner = self._runner_factory(self._policy)
                    try:
                        await runner.start()
                    except asyncio.CancelledError:
                        await self._shutdown_runner(runner)
                        raise
                    except (NativeSandboxError, UnsupportedPolicyError, OSError) as exc:
                        await self._shutdown_runner(runner)
                        raise NativeExecutionError(
                            f"Native sandbox is unavailable, refusing to run on the host: {exc}"
                        ) from exc
                    self._runner = runner
        return self._runner

    async def aclose(self) -> None:
        self._closed = True
        runner = self._runner
        await self._shutdown_runner(runner)
        self._runner = None

    @staticmethod
    async def _shutdown_runner(runner: NativeWorker | None) -> None:
        if runner is None:
            return
        cleanup = asyncio.create_task(runner.aclose())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    async def _call(self, operation: str, **params: object) -> dict:
        runner = await self.ensure_started()
        try:
            return await runner.call(operation, **params)
        except WorkerOperationError as exc:
            error = NativeExecutionError(f"Worker refused {operation}: {exc.message}")
            error.code = (
                "outcome_unknown" if operation in {"write_file", "edit_file"}
                and exc.code == "io_error" else exc.code
            )
            raise error from exc
        except NativeSandboxError as exc:
            error = NativeExecutionError(
                f"Native worker failed; no host fallback is available: {exc}"
            )
            error.code = (
                "outcome_unknown" if operation in {"write_file", "edit_file"}
                else "worker_failed"
            )
            raise error from exc

    def _resolve(self, cwd: Path | None, path: str) -> str:
        base = PurePosixPath((cwd or self._policy.workspace_root).as_posix())
        candidate = PurePosixPath(path)
        if candidate.is_absolute():
            return str(candidate)
        return str(base / candidate)

    @staticmethod
    def _failure(message: str, code: str = "execution_failed") -> ToolOutput:
        return ToolOutput(f"Error: {message}", True, {
            "code": code,
            **({"outcome_unknown": True} if code == "outcome_unknown" else {}),
        })

    async def read_file(
        self, path: str, limit: int | None = None, offset: int = 0,
        continuation: str | None = None, cwd: Path | None = None,
    ) -> ToolOutput:
        try:
            result = await self._call("read_file", path=self._resolve(cwd, path))
        except NativeExecutionError as exc:
            return self._failure(str(exc), getattr(exc, "code", "execution_failed"))
        raw = base64.b64decode(result["content_b64"], validate=True)
        base = cwd or self._policy.workspace_root
        relative = Path(self._resolve(cwd, path)).relative_to(base).as_posix()
        output = render_read_file(raw, base, relative, path, limit=limit,
                                  offset=offset, continuation=continuation)
        output.metadata["source_evidence"]["native_worker"] = True
        return output

    async def glob(self, pattern: str, cwd: Path | None = None) -> ToolOutput:
        try:
            result = await self._call("glob", pattern=pattern,
                                      cwd=str(cwd or self._policy.workspace_root))
        except NativeExecutionError as exc:
            return self._failure(str(exc), getattr(exc, "code", "execution_failed"))
        return ToolOutput(result["content"], False, {})

    async def write_file(self, path: str, content: str, cwd: Path | None = None) -> ToolOutput:
        try:
            result = await self._call(
                "write_file", path=self._resolve(cwd, path), overwrite=True,
                content_b64=base64.b64encode(content.encode("utf-8")).decode("ascii"),
            )
        except NativeExecutionError as exc:
            return self._failure(str(exc), getattr(exc, "code", "execution_failed"))
        return ToolOutput(f"Wrote {result['size']} bytes to {path}", False, {})

    async def edit_file(
        self, path: str, old_text: str, new_text: str,
        expected_sha256: str | None = None, cwd: Path | None = None,
    ) -> ToolOutput:
        try:
            await self._call(
                "edit_file", path=self._resolve(cwd, path), old_text=old_text,
                new_text=new_text, expected_sha256=expected_sha256,
            )
        except NativeExecutionError as exc:
            return self._failure(str(exc), getattr(exc, "code", "execution_failed"))
        return ToolOutput(f"Edited {path}", False, {})


def cleanup_native_tmp(policy: NativeSandboxPolicy) -> None:
    """Remove the owned temporary root; cleanup failures remain visible."""
    shutil.rmtree(policy.tmp_root)
