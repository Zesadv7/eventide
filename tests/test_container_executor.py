"""Docker lifecycle contracts using an offline executable fixture."""

import asyncio
import json
import os
import shutil
import sys

import pytest

from eventide.container_executor import ContainerCleanupError, ContainerCommandExecutor
from eventide.executor import ToolContext, ToolExecutor
from eventide.models import ToolCall
from eventide.policy import PolicyEngine


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("executable script fixture uses a POSIX shebang")
    directory = tmp_path / "docker-fixture"
    directory.mkdir()
    config, log = directory / "config.json", directory / "calls.jsonl"
    config.write_text("{}")
    script = directory / "docker"
    script.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json
import os
import pathlib
import sys
import time

directory = pathlib.Path(__file__).parent
config = json.loads((directory / "config.json").read_text())
args = sys.argv[1:]
with (directory / "calls.jsonl").open("a") as output:
    output.write(json.dumps({"args": args, "docker_host": os.getenv("DOCKER_HOST"),
                             "docker_context": os.getenv("DOCKER_CONTEXT")}) + "\n")
if args[0] == "--host":
    args = args[2:]
action = args[0]
if action == "context":
    if config.get("context_exit"):
        print("Docker context error", file=sys.stderr)
        sys.exit(config["context_exit"])
    print(config.get("raw_endpoint") or json.dumps(config.get("endpoint", "unix:///fixture.sock")))
elif action == "create":
    time.sleep(config.get("create_delay", 0))
    if config.get("create_exit"):
        print("Image unavailable; pull disabled", file=sys.stderr)
        sys.exit(config["create_exit"])
    name = args[args.index("--name") + 1]
    (directory / name).write_text(args[-1])
    print(config.get("container_id", "f" * 64))
elif action == "start":
    time.sleep(config.get("start_delay", 0))
    print(config.get("stdout", "done"), end="", flush=True)
    print(config.get("stderr", ""), end="", file=sys.stderr, flush=True)
    if config.get("auto_remove"):
        (directory / args[-1]).unlink()
    sys.exit(config.get("exit_code", 0))
elif action == "rm":
    time.sleep(config.get("cleanup_delay", 0))
    if config.get("cleanup_exit"):
        print(config.get("cleanup_error", "Daemon unavailable"), file=sys.stderr)
        sys.exit(config["cleanup_exit"])
    target = directory / args[-1]
    if not target.exists():
        print("Error response from daemon: No such container: " + args[-1], file=sys.stderr)
        sys.exit(1)
    target.unlink()
else:
    sys.exit("unexpected Docker action")
"""
    )
    script.chmod(0o700)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    return directory, script, config, log


def configure(fixture, **settings):
    fixture[2].write_text(json.dumps(settings))


def calls(fixture):
    log = fixture[3]
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def action(call):
    args = call["args"]
    return args[2] if args[0] == "--host" else args[0]


async def wait_action(fixture, requested):
    async def wait():
        while not any(action(call) == requested for call in calls(fixture)):
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 2)


def executor(fixture, **options):
    return ContainerCommandExecutor("python:3.12-slim", docker_path=fixture[1], **options)


async def test_exact_argv_boundary_is_readonly_and_does_not_execute_host_shell(
    fake_docker, tmp_path
):
    cwd = tmp_path / "project with spaces"
    cwd.mkdir()
    marker = tmp_path / "must-not-exist"
    command = f"printf '%s' `touch '{marker}'`; $(touch '{marker}'); echo \"$HOME\""
    result = await executor(fake_docker).execute(command, cwd)
    assert not marker.exists()
    assert result.exit_code == 0 and result.stdout == "done"
    observed = calls(fake_docker)
    assert [action(call) for call in observed] == ["context", "create", "start", "rm"]
    args = observed[1]["args"]
    name = args[args.index("--name") + 1]
    assert args[-3:] == ["python:3.12-slim", "-lc", command]
    assert args.count("--mount") == 1
    assert args[args.index("--mount") + 1] == (
        f"type=bind,src={cwd},dst=/workspace,bind-recursive=disabled,readonly"
    )
    for flag in (
        "--pull=never",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=1000:1000",
        "--init",
        "--no-healthcheck",
        "--workdir=/workspace",
        "--entrypoint=/bin/sh",
    ):
        assert flag in args
    assert args[args.index("--memory") + 1] == "512m"
    assert args[args.index("--memory-swap") + 1] == "512m"
    assert args[args.index("--pids-limit") + 1] == "128"
    assert "--tmpfs=/tmp:rw,nosuid,nodev,size=64m" in args
    assert "--env" not in args and "--env-file" not in args and "--privileged" not in args
    assert observed[2]["args"][-3:] == ["start", "--attach", name]
    assert observed[3]["args"][-4:] == ["rm", "--force", "--volumes", name]
    assert not (fake_docker[0] / name).exists()


async def test_writable_worktree_mount_requires_explicit_configuration(fake_docker, tmp_path):
    await executor(fake_docker, read_only=False, memory_mb=64, cpus=0.5, pids_limit=16).execute(
        "echo ok",
        tmp_path,
    )
    args = next(call["args"] for call in calls(fake_docker) if action(call) == "create")
    assert not args[args.index("--mount") + 1].endswith(",readonly")
    assert "--read-only" in args
    assert args[args.index("--memory") + 1] == "64m"
    assert args[args.index("--cpus") + 1] == "0.5"


async def test_output_status_and_bounds_reuse_command_result(fake_docker, tmp_path):
    configure(fake_docker, stdout="a" * 5000, stderr="z" * 4000, exit_code=7)
    result = await executor(fake_docker, output_limit=128).execute("ignored", tmp_path)
    assert result.is_error and result.exit_code == 7 and not result.timed_out
    assert result.stdout_bytes == 5000 and result.stderr_bytes == 4000
    assert result.stdout_omitted_bytes == 4872 and result.stderr_omitted_bytes == 3872
    assert len(result.stdout) < 250 and len(result.stderr) < 250
    assert result.duration_ms > 0


async def test_timeout_removes_container_after_client_termination(fake_docker, tmp_path):
    configure(fake_docker, start_delay=30)
    result = await executor(fake_docker, timeout=0.1).execute("sleep 30", tmp_path)
    assert result.timed_out and result.is_error
    assert [action(call) for call in calls(fake_docker)][-1] == "rm"
    assert not list(fake_docker[0].glob("eventide-*"))


async def test_repeated_cancellation_waits_for_container_cleanup(fake_docker, tmp_path):
    configure(fake_docker, start_delay=30, cleanup_delay=0.1)
    task = asyncio.create_task(executor(fake_docker).execute("sleep 30", tmp_path))
    await wait_action(fake_docker, "start")
    task.cancel()
    await wait_action(fake_docker, "rm")
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0.005)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(fake_docker[0].glob("eventide-*"))


async def test_stop_during_create_never_starts_later_container(fake_docker, tmp_path):
    configure(fake_docker, create_delay=0.08)
    task = asyncio.create_task(executor(fake_docker).execute("side effect", tmp_path))
    await wait_action(fake_docker, "create")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [action(call) for call in calls(fake_docker)] == ["context", "create", "rm"]
    assert not list(fake_docker[0].glob("eventide-*"))


async def test_stop_during_process_startup_retains_client_ownership(
    fake_docker, tmp_path, monkeypatch
):
    configure(fake_docker, start_delay=30)
    started = asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **options):
        process = await spawn(*args, **options)
        if "start" in args:
            started.set()
            await asyncio.sleep(0.05)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(executor(fake_docker).execute("sleep 30", tmp_path))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(fake_docker[0].glob("eventide-*"))
    assert [action(call) for call in calls(fake_docker)][-1] == "rm"


async def test_stop_during_final_cleanup_is_preserved(fake_docker, tmp_path):
    configure(fake_docker, cleanup_delay=0.1)
    task = asyncio.create_task(executor(fake_docker).execute("echo done", tmp_path))
    await wait_action(fake_docker, "rm")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(fake_docker[0].glob("eventide-*"))


async def test_cleanup_timeout_or_unrelated_missing_name_is_not_accepted(fake_docker, tmp_path):
    configure(fake_docker, cleanup_delay=30)
    with pytest.raises(ContainerCleanupError, match="cleanup was not confirmed"):
        await executor(fake_docker, cleanup_timeout=0.5).execute("echo done", tmp_path)
    configure(fake_docker, cleanup_exit=1, cleanup_error="No such container: some-other-name")
    with pytest.raises(ContainerCleanupError, match="cleanup was not confirmed"):
        await executor(fake_docker).execute("echo done", tmp_path)


async def test_parallel_commands_have_distinct_cleanup_identities(fake_docker, tmp_path):
    runner = executor(fake_docker)
    results = await asyncio.gather(
        runner.execute("first", tmp_path), runner.execute("second", tmp_path)
    )
    assert all(result.exit_code == 0 for result in results)
    created = [call["args"] for call in calls(fake_docker) if action(call) == "create"]
    names = {args[args.index("--name") + 1] for args in created}
    assert len(names) == 2
    assert not list(fake_docker[0].glob("eventide-*"))


async def test_creation_timeout_is_not_reported_as_known_clean_execution(
    fake_docker, tmp_path, monkeypatch
):
    monkeypatch.setenv("DOCKER_HOST", "unix:///fixture/docker.sock")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    configure(fake_docker, create_delay=30)
    with pytest.raises(ContainerCleanupError, match="creation was not confirmed"):
        await executor(fake_docker, cleanup_timeout=0.5).execute("ignored", tmp_path)
    assert [action(call) for call in calls(fake_docker)] == ["create", "rm"]


async def test_missing_image_is_an_error_without_pull_or_start(fake_docker, tmp_path):
    configure(fake_docker, create_exit=125)
    result = await executor(fake_docker).execute("ignored", tmp_path)
    assert result.exit_code == 125 and "Image unavailable" in result.stderr
    assert [action(call) for call in calls(fake_docker)] == ["context", "create", "rm"]
    assert all("pull" not in call["args"] for call in calls(fake_docker))


async def test_auto_removal_is_distinguished_from_cleanup_failure(fake_docker, tmp_path):
    configure(fake_docker, auto_remove=True)
    assert not (await executor(fake_docker).execute("ignored", tmp_path)).is_error
    configure(fake_docker, cleanup_exit=7)
    with pytest.raises(ContainerCleanupError, match="cleanup was not confirmed"):
        await executor(fake_docker).execute("ignored", tmp_path)


async def test_bad_container_identity_still_attempts_cleanup(fake_docker, tmp_path):
    configure(fake_docker, container_id="invalid")
    with pytest.raises(ContainerCleanupError, match="did not confirm container identity"):
        await executor(fake_docker).execute("ignored", tmp_path)
    assert not list(fake_docker[0].glob("eventide-*"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "ssh://remote",
        "tcp://localhost:2375",
        "http://host:2375",
        "npipe:////remote-server/pipe/docker_engine",
        "npipe://arbitrary-host",
    ],
)
async def test_remote_daemon_is_rejected_before_creating_anything(fake_docker, tmp_path, endpoint):
    configure(fake_docker, endpoint=endpoint)
    with pytest.raises(ValueError, match="local Docker"):
        await executor(fake_docker).execute("ignored", tmp_path)
    assert [action(call) for call in calls(fake_docker)] == ["context"]


async def test_local_windows_named_pipe_is_supported(fake_docker, tmp_path):
    endpoint = "npipe:////./pipe/docker_engine"
    configure(fake_docker, endpoint=endpoint)
    await executor(fake_docker).execute("ignored", tmp_path)
    assert calls(fake_docker)[1]["args"][:2] == ["--host", endpoint]


async def test_explicit_local_endpoint_is_frozen_across_all_operations(
    fake_docker, tmp_path, monkeypatch
):
    monkeypatch.setenv("DOCKER_HOST", "unix:///selected.sock")
    await executor(fake_docker).execute("ignored", tmp_path)
    assert [action(call) for call in calls(fake_docker)] == ["create", "start", "rm"]
    for call in calls(fake_docker):
        assert call["args"][:2] == ["--host", "unix:///selected.sock"]
        assert call["docker_host"] is None and call["docker_context"] is None


async def test_context_override_wins_over_remote_host_env(fake_docker, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    await executor(fake_docker).execute("ignored", tmp_path)
    assert calls(fake_docker)[1]["args"][:2] == ["--host", "unix:///fixture.sock"]


@pytest.mark.parametrize("settings", [{"context_exit": 1}, {"raw_endpoint": "invalid-json"}])
async def test_context_errors_are_clear(fake_docker, tmp_path, settings):
    configure(fake_docker, **settings)
    with pytest.raises(RuntimeError, match="Docker context"):
        await executor(fake_docker).execute("ignored", tmp_path)


async def test_missing_docker_is_explicit_without_local_shell_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Docker executable is unavailable"):
        await ContainerCommandExecutor("python:3.12-slim").execute("echo no", tmp_path)


async def test_cwd_symlink_and_mount_delimiters_are_rejected(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(original, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    runner = ContainerCommandExecutor("python:3.12-slim")
    for cwd, match in (
        (link, "symlink"),
        (link / "subdir", "symlink"),
        (tmp_path / "missing", "existing directory"),
    ):
        with pytest.raises(ValueError, match=match):
            await runner.execute("ignored", cwd)
    odd = tmp_path / "with,comma"
    odd.mkdir()
    with pytest.raises(ValueError, match="mount delimiters"):
        await runner.execute("ignored", odd)


@pytest.mark.parametrize(
    "image", ["", "--privileged", "bad image", "a; touch /tmp/no", "a\x00b", "https://image", "a:"]
)
def test_invalid_image_reference_is_rejected(image):
    with pytest.raises(ValueError, match="explicit Docker image"):
        ContainerCommandExecutor(image)


@pytest.mark.parametrize(
    "setting",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"cleanup_timeout": 61},
        {"memory_mb": 0},
        {"memory_mb": True},
        {"pids_limit": -1},
        {"cpus": 0},
        {"cpus": float("inf")},
        {"output_limit": 1},
        {"read_only": "false"},
    ],
)
def test_resource_limits_are_positive_finite_and_bounded(setting):
    with pytest.raises(ValueError):
        ContainerCommandExecutor("python:3.12-slim", **setting)


async def test_policy_still_gates_command_before_docker(fake_docker, tmp_path):
    tool_executor = ToolExecutor({"bash": lambda: "unused"}, command_executor=executor(fake_docker))
    result = await tool_executor.execute(
        ToolCall("blocked", "bash", {"command": "rm -rf /"}),
        ToolContext(tmp_path, "run", PolicyEngine()),
    )
    assert result.is_error and "Permission denied" in result.content
    assert not calls(fake_docker)


@pytest.mark.skipif(
    not os.getenv("EVENTIDE_TEST_CONTAINER_IMAGE"), reason="real Docker is explicit opt-in"
)
async def test_real_local_container_readonly_boundary(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker executable is unavailable")
    # Writable POSIX permissions make the readonly mount, rather than UID mismatch,
    # responsible for rejecting writes. This is a disposable test directory.
    tmp_path.chmod(0o777)
    runner = ContainerCommandExecutor(os.environ["EVENTIDE_TEST_CONTAINER_IMAGE"])
    result = await runner.execute(
        "id -u; if touch /workspace/should-not-write; then exit 23; fi; "
        "if touch /should-not-write; then exit 24; fi; test -w /tmp",
        tmp_path,
    )
    assert result.exit_code == 0 and "1000" in result.stdout
    assert not (tmp_path / "should-not-write").exists()
