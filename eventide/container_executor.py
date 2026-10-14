"""Explicit Docker boundary for commands, independent of Host model/MCP calls."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from eventide.executor import LocalCommandExecutor, _BoundedOutput
from eventide.models import CommandResult

_T = TypeVar("_T")


class ContainerCleanupError(RuntimeError):
    """Docker did not confirm cleanup; the caller must retain this uncertainty."""


class _DockerClient(LocalCommandExecutor):
    async def run(self, args: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        """Pass argv directly; reuse local bounded collection and process cleanup."""
        started = time.monotonic()
        options: dict[str, Any] = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process, cancelled = await _owned(
            asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *args,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **options,
                )
            )
        )
        stdout, stderr = _BoundedOutput(self.output_limit), _BoundedOutput(self.output_limit)
        collection = asyncio.create_task(self._collect(process, stdout, stderr))
        timed_out = False
        try:
            if cancelled:
                raise asyncio.CancelledError
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


async def _owned(task: asyncio.Task[_T]) -> tuple[_T, bool]:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


class ContainerCommandExecutor:
    """Run a command in a local Docker Linux container with bounded resources.

    Requires an explicitly selected, already available image containing /bin/sh.
    Only cwd is bind-mounted; writable mounts must be explicitly requested.
    Docker Desktop local sockets are supported, remote daemons are rejected.
    This isolates this command's process/filesystem access, not the whole Agent:
    Python file tools, the Provider, MCP and the Docker daemon remain on the host.
    """

    def __init__(
        self,
        image: str,
        *,
        timeout: float = 120,
        output_limit: int = 25_000,
        read_only: bool = True,
        memory_mb: int = 512,
        cpus: float = 1,
        pids_limit: int = 128,
        cleanup_timeout: float = 10,
        docker_path: str | Path | None = None,
    ):
        if (
            not isinstance(image, str)
            or not 1 <= len(image) <= 256
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", image)
            or "//" in image
            or image.endswith(("/", ":", "@"))
        ):
            raise ValueError("A valid explicit Docker image reference is required")
        if not isinstance(read_only, bool):
            raise ValueError("read_only must be a boolean")
        for name, value, minimum, maximum in (
            ("timeout", timeout, 0, 86_400),
            ("cleanup_timeout", cleanup_timeout, 0, 60),
            ("cpus", cpus, 0, 16),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not minimum < value <= maximum
            ):
                raise ValueError(f"{name} must be positive and at most {maximum}")
        for name, value, minimum, maximum in (
            ("memory_mb", memory_mb, 6, 16_384),
            ("pids_limit", pids_limit, 1, 1024),
            ("output_limit", output_limit, 2, 1_000_000),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        self.image, self.read_only = image, read_only
        self.memory_mb, self.cpus, self.pids_limit = memory_mb, cpus, pids_limit
        self.docker_path = str(docker_path) if docker_path is not None else "docker"
        self._commands = _DockerClient(timeout, output_limit)
        self._control = _DockerClient(cleanup_timeout, 25_000)

    @staticmethod
    def _cwd(cwd: Path) -> Path:
        absolute = cwd.absolute()
        if ".." in absolute.parts or any(
            part.is_symlink() for part in (absolute, *absolute.parents)
        ):
            raise ValueError("Container cwd cannot contain symlink or parent traversal components")
        if not absolute.is_dir():
            raise ValueError("Container cwd must be an existing directory")
        if any(character in str(absolute) for character in (",", "\n", "\r", "\x00")):
            raise ValueError("Container cwd contains unsupported Docker mount delimiters")
        return absolute

    async def _endpoint(self, docker: str, cwd: Path, env: dict[str, str]) -> str:
        endpoint = env.get("DOCKER_HOST") if not env.get("DOCKER_CONTEXT") else None
        if not endpoint:
            result = await self._control.run(
                [
                    docker,
                    "context",
                    "inspect",
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ],
                cwd,
                env,
            )
            if result.is_error:
                raise RuntimeError(
                    f"Cannot inspect Docker context: {result.stderr or result.render()}"
                )
            try:
                endpoint = json.loads(result.stdout)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Docker context returned an invalid endpoint") from exc
        if not isinstance(endpoint, str) or not endpoint.startswith(("unix:///", "npipe://")):
            raise ValueError("Container commands require a local Docker unix:// or npipe:// daemon")
        return endpoint

    def _create_args(self, base: list[str], cwd: Path, name: str, command: str) -> list[str]:
        mount = f"type=bind,src={cwd},dst=/workspace,bind-recursive=disabled"
        if self.read_only:
            mount += ",readonly"
        memory = f"{self.memory_mb}m"
        return [
            *base,
            "create",
            "--pull=never",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            str(self.cpus),
            "--user=1000:1000",
            "--init",
            "--no-healthcheck",
            "--mount",
            mount,
            "--workdir=/workspace",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=64m",
            "--entrypoint=/bin/sh",
            self.image,
            "-lc",
            command,
        ]

    async def _remove(self, base: list[str], cwd: Path, env: dict[str, str], name: str) -> bool:
        result, cancelled = await _owned(
            asyncio.create_task(
                self._control.run([*base, "rm", "--force", "--volumes", name], cwd, env)
            )
        )
        already_removed = (
            not result.timed_out and "No such container" in result.stderr and name in result.stderr
        )
        if result.is_error and not already_removed:
            raise ContainerCleanupError(
                f"Container cleanup was not confirmed for {name}; inspect Docker: "
                f"{result.stderr or result.render()}"
            )
        return cancelled

    async def execute(self, command: str, cwd: Path) -> CommandResult:
        if not isinstance(command, str) or "\x00" in command:
            raise ValueError("Container command must be text without NUL")
        cwd = self._cwd(cwd)
        docker = shutil.which(self.docker_path)
        if docker is None:
            raise RuntimeError(
                "Docker executable is unavailable; install Docker and select a local image"
            )
        env = dict(os.environ)
        endpoint = await self._endpoint(docker, cwd, env)
        # Freeze the selected daemon for creation, execution and cleanup.
        env.pop("DOCKER_CONTEXT", None)
        env.pop("DOCKER_HOST", None)
        base = [docker, "--host", endpoint]
        name = f"eventide-{uuid.uuid4().hex}"
        started = time.monotonic()
        result: CommandResult
        try:
            # A cancelled create request can arrive at the daemon after an early
            # rm said "not found". Wait for create to settle before any start/rm.
            creation, cancelled = await _owned(
                asyncio.create_task(
                    self._control.run(self._create_args(base, cwd, name, command), cwd, env)
                )
            )
            if creation.timed_out:
                raise ContainerCleanupError(
                    f"Container creation was not confirmed for {name}; "
                    "inspect Docker before retrying"
                )
            if cancelled:
                raise asyncio.CancelledError
            if creation.is_error:
                result = creation
            elif not re.fullmatch(r"[a-f0-9]{12,64}", creation.stdout.strip()):
                raise ContainerCleanupError(f"Docker did not confirm container identity for {name}")
            else:
                result = await self._commands.run([*base, "start", "--attach", name], cwd, env)
        finally:
            cleanup_cancelled = await self._remove(base, cwd, env, name)
        if cleanup_cancelled:
            raise asyncio.CancelledError
        return replace(result, duration_ms=(time.monotonic() - started) * 1000)
