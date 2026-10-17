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


async def test_real_optional_sandbox_edits_tests_and_exports_patch(isolated_workspace, image):
    """A useful offline task crosses file + shell boundaries and leaves source untouched."""
    from dataclasses import replace

    from tests.test_host import make_repo

    root = make_repo(isolated_workspace / "sandbox-source")
    (root / "answer.py").write_text("def answer():\n    return 41\n")
    (root / "test_answer.py").write_text(
        "import unittest\nfrom answer import answer\n"
        "class TestAnswer(unittest.TestCase):\n"
        "    def test_answer(self):\n        self.assertEqual(answer(), 42)\n"
    )
    (root / ".env").write_text("DO_NOT_MOUNT=secret\n")
    subprocess.run(["git", "-C", str(root), "add", "answer.py", "test_answer.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "sandbox task fixture"], check=True)
    provider = ScriptedProvider([
        call("read_file", {"path": "answer.py"}, "read"),
        call("edit_file", {"path": "answer.py", "old_text": "return 41",
                           "new_text": "return 42"}, "edit"),
        call("bash", {"command": "python3 -m unittest -v && test ! -e .git && "
                      "test ! -e .env && test ! -d __pycache__ && "
                      "test ! -e /var/run/docker.sock"}, "test"),
        {"text": "Candidate test passed; review exported patch."},
    ])
    host = RuntimeHost(
        replace(settings_for(root), state_dir=isolated_workspace / "sandbox-state"), provider,
    )
    try:
        result = await host.run(RunRequest(
            "fix answer and verify", mode="agent", execution_profile="sandbox", sandbox_image=image,
        ))
        assert result.status == "completed"
        events = host.store.run_events(result.run_id)
        results = [event["payload"] for event in events if event["type"] == "tool.completed"]
        assert len(results) == 3 and all(not item["is_error"] for item in results), results
        assert "OK" in results[-1]["content"]
        assert (root / "answer.py").read_text() == "def answer():\n    return 41\n"
        assert (root / ".env").read_text() == "DO_NOT_MOUNT=secret\n"
        binding = host._execution_binding(result.run_id)
        assert binding is not None
        patch = host.execution_sandboxes.diff(binding)
        assert "+    return 42" in patch and ".env" not in patch
        checked = subprocess.run(
            ["git", "-C", str(root), "apply", "--check", "-"], input=patch,
            text=True, capture_output=True,
        )
        assert checked.returncode == 0, checked.stderr
    finally:
        await host.close()
