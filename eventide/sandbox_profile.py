"""Fail-closed admission for the optional, offline Docker execution profile."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from eventide.container_executor import ContainerCommandExecutor

LIMITS: dict[str, Any] = {
    "kind": "sandbox",
    "network": "none",
    "memory_mb": 512,
    "cpus": 1,
    "pids_limit": 128,
    "timeout": 120,
    "output_limit": 25_000,
    "disable_mcp": True,
}


def validate_sandbox_profile(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {*LIMITS, "image", "requested_image"}:
        raise ValueError("Sandbox execution profile is incomplete or unsupported")
    for key, expected in LIMITS.items():
        if type(value[key]) is not type(expected) or value[key] != expected:
            raise ValueError(f"Sandbox execution profile has invalid {key}")
    if not isinstance(value["image"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        value["image"],
    ):
        raise ValueError("Sandbox requires an immutable local image identity")
    ContainerCommandExecutor(value["requested_image"])
    return dict(value)


async def resolve_sandbox_profile(image: str, root: Path) -> dict[str, Any]:
    executor = ContainerCommandExecutor(image)
    docker = shutil.which("docker")
    if not docker:
        raise ValueError(
            "Sandbox requires Docker and a preinstalled image with python3 and /bin/sh"
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "SystemRoot",
            "USERPROFILE",
        }
    }
    try:
        endpoint = await executor._endpoint(docker, root, env)
    except (RuntimeError, OSError) as exc:
        raise ValueError("Sandbox local Docker daemon is unavailable") from exc
    env.pop("DOCKER_CONTEXT", None)
    env.pop("DOCKER_HOST", None)
    result = await executor._control.run(
        [docker, "--host", endpoint, "image", "inspect", "--format", "{{.Id}}", image],
        root,
        env,
    )
    if result.is_error:
        raise ValueError(
            "Sandbox image is unavailable locally; install dependencies in the image first"
        )
    return validate_sandbox_profile(
        {
            **LIMITS,
            "image": result.stdout.strip(),
            "requested_image": image,
        }
    )


class SandboxCommandExecutor:
    """Mount only the admitted task root, including for nested working directories."""

    backend_kind = "container"

    def __init__(self, delegate: Any, root: Path):
        self.delegate, self.root = delegate, root

    async def execute(self, command: str, cwd: Path) -> Any:
        import shlex

        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise ValueError("Sandbox root identity changed")
        if cwd.resolve() != cwd or not cwd.is_relative_to(self.root):
            raise ValueError("Sandbox command working directory escapes its task copy")
        relative = cwd.relative_to(self.root).as_posix()
        return await self.delegate.execute(
            "export PYTHONDONTWRITEBYTECODE=1; "
            f"cd {shlex.quote('/workspace/' + relative)} && {command}",
            self.root,
        )
