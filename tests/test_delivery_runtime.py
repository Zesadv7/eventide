"""Independent checks bind a candidate patch to a protected, unchanged baseline."""

import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def delivery_host(path, script=()):
    source = make_repo(path / "repo")
    (source / "calc.py").write_text("def discount(price, fraction):\n    return price + fraction\n")
    (source / "tests").mkdir()
    (source / "tests/test_tokens.py").write_text(
        "import unittest\nfrom calc import discount\n"
        "class Check(unittest.TestCase):\n"
        "    def test_discount(self):\n        self.assertEqual(discount(100, .2), 80)\n"
    )
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return RuntimeHost(
        replace(settings_for(source), state_dir=path / "state"), ScriptedProvider(list(script))
    )


CHECK = shlex.quote(sys.executable) + " -B -m unittest discover -s tests"


async def test_writer_check_and_adoption_are_independent_and_durable(isolated_workspace):
    script = [
        {
            "tool_calls": [
                {
                    "id": "fix",
                    "name": "edit_file",
                    "arguments": {
                        "path": "calc.py",
                        "old_text": "price + fraction",
                        "new_text": "price * (1 - fraction)",
                    },
                }
            ]
        },
        {"text": "model claim: done"},
    ]
    host = delivery_host(isolated_workspace, script)
    prepared = await host.manage_delivery("prepare", name="repair")
    delivery_id = prepared["delivery_id"]
    try:
        with pytest.raises(ValueError, match="no passing"):
            await host.manage_delivery("apply", delivery_id=delivery_id)
        before = await host.manage_delivery("verify", delivery_id=delivery_id, checks=[CHECK])
        assert before["status"] == "failed"
        work = await host.manage_delivery("run", delivery_id=delivery_id, goal="fix discount")
        assert work["status"] == "completed"
        assert "price + fraction" in (host.settings.workdir / "calc.py").read_text()
        checked = await host.manage_delivery("verify", delivery_id=delivery_id, checks=[CHECK])
        assert checked["status"] == "passed"
        assert checked["checks"][0]["metadata"]["exit_code"] == 0
        assert Path(checked["artifact"]).read_bytes()
        state, repo = host.state_dir, host.settings.workdir
        await host.close()
        host = RuntimeHost(replace(settings_for(repo), state_dir=state), ScriptedProvider([]))
        shown = await host.manage_delivery("show", delivery_id=delivery_id)
        assert shown["verification"]["status"] == "passed"
        result = await host.manage_delivery("apply", delivery_id=delivery_id)
        assert result["patch_sha256"] == checked["patch_sha256"]
        assert "price * (1 - fraction)" in (repo / "calc.py").read_text()
        assert (
            subprocess.run(["git", "-C", str(repo), "diff", "--cached"], capture_output=True).stdout
            == b""
        )
        with pytest.raises(ValueError, match="already applied"):
            await host.manage_delivery("apply", delivery_id=delivery_id)
    finally:
        await host.close()


async def test_protected_tests_cannot_be_changed_to_fake_passing_checks(isolated_workspace):
    host = delivery_host(isolated_workspace)
    try:
        prepared = await host.manage_delivery("prepare", name="forged")
        checkout = Path(prepared["receipt"]["checkout"])
        (checkout / "tests/test_tokens.py").write_text("# deleted failing test\n")
        with pytest.raises(ValueError, match="Protected"):
            await host.manage_delivery(
                "verify", delivery_id=prepared["delivery_id"], checks=[CHECK]
            )
        assert host.store.delivery_facts(prepared["delivery_id"])[-1]["type"] != "delivery.verified"
    finally:
        await host.close()


async def test_checks_that_change_source_invalidate_the_receipt(isolated_workspace):
    host = delivery_host(isolated_workspace)
    try:
        prepared = await host.manage_delivery("prepare", name="changing-check")
        command = (
            shlex.quote(sys.executable)
            + " -B -c "
            + shlex.quote(
                "from pathlib import Path; Path('calc.py').write_text('changed by check')"
            )
        )
        with pytest.raises(ValueError, match="changed during"):
            await host.manage_delivery(
                "verify", delivery_id=prepared["delivery_id"], checks=[command]
            )
    finally:
        await host.close()


async def test_user_changes_after_verification_block_apply(isolated_workspace):
    host = delivery_host(isolated_workspace)
    try:
        prepared = await host.manage_delivery("prepare", name="stale")
        checkout = Path(prepared["receipt"]["checkout"])
        (checkout / "calc.py").write_text(
            "def discount(price, fraction):\n    return price * (1-fraction)\n"
        )
        checked = await host.manage_delivery(
            "verify", delivery_id=prepared["delivery_id"], checks=[CHECK]
        )
        assert checked["status"] == "passed"
        (host.settings.workdir / "user.txt").write_text("keep my work")
        with pytest.raises(ValueError, match="changed"):
            await host.manage_delivery("apply", delivery_id=prepared["delivery_id"])
        assert (host.settings.workdir / "user.txt").read_text() == "keep my work"
    finally:
        await host.close()


async def test_empty_container_selection_cannot_fall_back_to_local(isolated_workspace):
    host = delivery_host(isolated_workspace)
    try:
        prepared = await host.manage_delivery("prepare", name="no-fallback")
        with pytest.raises(ValueError, match="image cannot be empty"):
            await host.manage_delivery(
                "run", delivery_id=prepared["delivery_id"], goal="fix", image=""
            )
        assert host.provider.requests == []
    finally:
        await host.close()


async def test_container_profile_survives_continue_restart_and_new_session(
    isolated_workspace, monkeypatch
):
    from eventide import container_executor
    from eventide.models import CommandResult, RunRequest

    selected = []

    class Container:
        def __init__(self, image, **kwargs):
            selected.append((image, kwargs["read_only"]))

        async def execute(self, command, cwd):
            return CommandResult(0, stdout="checked inside selected backend")

    monkeypatch.setattr(container_executor, "ContainerCommandExecutor", Container)
    host = delivery_host(
        isolated_workspace,
        [
            {
                "tool_calls": [
                    {
                        "id": "fix",
                        "name": "edit_file",
                        "arguments": {
                            "path": "calc.py",
                            "old_text": "price + fraction",
                            "new_text": "price * (1-fraction)",
                        },
                    }
                ]
            }
        ],
    )
    host.settings = replace(host.settings, max_steps=1)
    try:
        prepared = await host.manage_delivery("prepare", name="profile")
        work = await host.manage_delivery(
            "run", delivery_id=prepared["delivery_id"], goal="fix", image="demo:local"
        )
        assert work["status"] == "interrupted"
        state, repo = host.state_dir, host.settings.workdir
        await host.close()
        script = [
            {"tool_calls": [{"id": "check", "name": "bash", "arguments": {"command": "test"}}]},
            {"text": "done"},
            {"tool_calls": [{"id": "another", "name": "bash", "arguments": {"command": "test"}}]},
            {"text": "new goal done"},
        ]
        host = RuntimeHost(replace(settings_for(repo), state_dir=state), ScriptedProvider(script))
        assert (await host.continue_session(work["worker_session_id"])).status == "completed"
        workspace = host.resolve_or_register_workspace(Path(work["checkout"]))
        session = host.create_session(workspace_id=workspace.workspace_id)
        assert (await host.run(RunRequest("new goal", session))).status == "completed"
        assert selected == [("demo:local", False)] * 3
    finally:
        await host.close()
