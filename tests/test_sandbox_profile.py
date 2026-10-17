"""Offline admission and immutable execution profile contract."""

from pathlib import Path

import pytest

from eventide.models import CommandResult
from eventide.sandbox_profile import (
    LIMITS,
    SandboxCommandExecutor,
    resolve_sandbox_profile,
    validate_sandbox_profile,
)

IMAGE = "sha256:" + "a" * 64


def profile():
    return {**LIMITS, "image": IMAGE, "requested_image": "eventide-tools:local"}


@pytest.mark.parametrize(
    "change",
    [
        {"network": "host"},
        {"disable_mcp": False},
        {"cpus": True},
        {"timeout": 0},
        {"image": "python:latest"},
        {"extra": "ignored"},
        {"requested_image": "--privileged"},
    ],
)
def test_persisted_profile_rejects_weakening(change):
    with pytest.raises(ValueError):
        validate_sandbox_profile({**profile(), **change})


def test_complete_profile_required():
    assert validate_sandbox_profile(profile()) == profile()
    with pytest.raises(ValueError):
        validate_sandbox_profile({})


async def test_admission_pins_local_image_and_filters_host_secrets(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("eventide.sandbox_profile.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setenv("OPENAI_API_KEY", "private-provider-key")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    async def run(self, args, cwd, env):
        calls.append((args, env))
        return CommandResult(exit_code=0, stdout=IMAGE + "\n", stderr="")

    monkeypatch.setattr("eventide.container_executor._DockerClient.run", run)
    result = await resolve_sandbox_profile("eventide-tools:local", tmp_path)
    assert result == profile()
    assert calls[0][0] == [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        "eventide-tools:local",
    ]
    assert "OPENAI_API_KEY" not in calls[0][1]
    assert "DOCKER_HOST" not in calls[0][1]


async def test_missing_docker_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("eventide.sandbox_profile.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="requires Docker"):
        await resolve_sandbox_profile("python:local", tmp_path)


async def test_remote_daemon_rejected_without_inspection(tmp_path, monkeypatch):
    monkeypatch.setattr("eventide.sandbox_profile.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setenv("DOCKER_HOST", "tcp://example.test:2375")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    with pytest.raises(ValueError, match="local Docker"):
        await resolve_sandbox_profile("python:local", tmp_path)


async def test_nested_command_mounts_only_root(tmp_path):
    calls = []

    class Delegate:
        async def execute(self, command, cwd):
            calls.append((command, cwd))
            return "ok"

    root = tmp_path / "sandbox"
    nested = root / "sub dir"
    nested.mkdir(parents=True)
    executor = SandboxCommandExecutor(Delegate(), root)
    assert await executor.execute("printf ok", nested) == "ok"
    assert calls == [
        ("export PYTHONDONTWRITEBYTECODE=1; cd '/workspace/sub dir' && printf ok", root)
    ]
    with pytest.raises(ValueError, match="escapes"):
        await executor.execute("echo unsafe", Path("/tmp"))
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        await executor.execute("echo unsafe", root / "escape")
