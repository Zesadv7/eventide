"""Explicit real Docker checks; ordinary offline pytest never starts a daemon."""

import asyncio
import os
import shutil
import subprocess

import pytest

from eventide.container_executor import ContainerCommandExecutor
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for
from tests.test_skill_runtime import call, package

pytestmark = pytest.mark.skipif(
    not os.getenv("EVENTIDE_TEST_CONTAINER_IMAGE"), reason="real Docker is explicit opt-in"
)


@pytest.fixture
def image():
    assert shutil.which("docker"), "Explicit Docker verification requires the Docker CLI"
    return os.environ["EVENTIDE_TEST_CONTAINER_IMAGE"]


@pytest.mark.parametrize("suffix", ["py", "sh"])
@pytest.mark.parametrize("git_project", [False, True])
async def test_real_skill_script_uses_frozen_resources_and_approved_container(
    isolated_workspace, image, suffix, git_project
):
    root = isolated_workspace.resolve()
    root.chmod(0o777)
    if git_project:
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    source = package(root / "skills/diagnose", scripts=True)
    path = f"scripts/check.{suffix}"
    script = (
        "import os, sys\nfrom pathlib import Path\n"
        "assert os.getuid() == 1000\n"
        "assert Path(os.environ['EVENTIDE_SKILL_ROOT'], 'references/check.md').read_text() "
        "== 'version one'\n"
        "assert sys.argv[1] == 'literal; touch injected'\n"
        "Path('script-ran').write_text('approved container')\nprint('container verified')\n"
        if suffix == "py"
        else 'test "$(id -u)" = 1000 && '
        'test "$(cat "$EVENTIDE_SKILL_ROOT/references/check.md")" = "version one" && '
        'test "$1" = "literal; touch injected" && '
        "printf 'approved container' > script-ran && echo 'container verified'\n"
    )
    (source / path).write_text(script)
    provider = ScriptedProvider(
        [
            call("load_skill", {"name": "diagnose"}, "load"),
            call(
                "run_skill_script",
                {"name": "project:diagnose", "path": path, "args": ["literal; touch injected"]},
                "script",
            ),
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(root), provider)
    host.executor.command_executor = ContainerCommandExecutor(image, read_only=False)
    approvals = []

    async def approve(identity, tool, reason):
        approvals.append(tool.name)
        return True

    old_umask = os.umask(0o077)
    try:
        result = await host.run(RunRequest("run packaged check"), approval_handler=approve)
        results = [
            e
            for e in host.store.run_events(result.run_id)
            if e["type"] == "tool.completed" and e["payload"]["name"] == "run_skill_script"
        ]
        assert len(results) == 1
        assert not results[0]["payload"]["is_error"], results[0]["payload"]
        assert (root / "script-ran").read_text() == "approved container"
        assert approvals == ["run_skill_script"]
        assert not (root / "injected").exists()
        assert not list(root.glob(".eventide-skill-*"))
    finally:
        os.umask(old_umask)
        await host.close()


def containers():
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=eventide-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return set(result.stdout.split())


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_timeout_and_stop_wait_for_cleanup(isolated_workspace, image, cancel):
    root = isolated_workspace.resolve()
    root.chmod(0o777)
    before = containers()
    runner = ContainerCommandExecutor(image, read_only=False, timeout=30 if cancel else 2)
    task = asyncio.create_task(runner.execute("echo started > ready; sleep 30", root))
    try:

        async def started():
            while not (root / "ready").exists():
                if task.done():
                    raise AssertionError(f"Container ended before start marker: {task.result()}")
                await asyncio.sleep(0.05)

        await asyncio.wait_for(started(), 15)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.timed_out
        assert containers() == before
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
