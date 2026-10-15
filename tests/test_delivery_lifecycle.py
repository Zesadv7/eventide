"""Stop and stale state cannot weaken source ownership or verification evidence."""

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from eventide.container_executor import ContainerCleanupError
from eventide.host import RuntimeHost
from eventide.models import CommandResult, RunRequest
from eventide.providers import ScriptedProvider
from tests.test_delivery_runtime import CHECK, delivery_host
from tests.test_host import make_repo
from tests.test_runtime import settings_for


async def test_repeated_stop_and_close_wait_for_sync_write(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(5)
        (repo / "late.txt").write_text("finished before ownership release")
        finished.set()
        return "written"

    host = RuntimeHost(
        settings,
        ScriptedProvider([{"tool_calls": [{"id": "slow", "name": "slow", "arguments": {}}]}]),
        tools=[{"name": "slow", "input_schema": {"type": "object", "properties": {}}}],
        handlers={"slow": slow},
    )
    session = host.create_session()
    task = asyncio.create_task(host.run(RunRequest("write slowly", session)))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        closing = asyncio.create_task(host.close())
        await asyncio.sleep(0.02)
        assert not task.done() and not closing.done() and not finished.is_set()
        assert host.workspace_busy(host.default_workspace.workspace_id)
        with pytest.raises(RuntimeError, match="already owned"):
            RuntimeHost(settings, ScriptedProvider([]))
        release.set()
        await asyncio.wait_for(closing, 5)
        assert task.cancelled() and finished.is_set()
        reopened = RuntimeHost(settings, ScriptedProvider([{"text": "resumed"}]))
        try:
            run = reopened.store.latest_run(session)
            assert run["tools"]["slow"]["status"] == "completed"
            assert (await reopened.continue_session(session)).status == "completed"
        finally:
            await reopened.close()
    finally:
        release.set()
        if closing:
            await closing
        else:
            await host.close()


async def test_prepare_rejects_dirty_source_before_creating_worktree(isolated_workspace):
    host = delivery_host(isolated_workspace)
    try:
        (host.settings.workdir / "user.txt").write_text("my unfinished work")
        with pytest.raises(ValueError, match="clean"):
            await host.manage_delivery("prepare", name="unused")
        assert await host.manage_worktree(host.default_workspace.workspace_id, "list") == []
    finally:
        await host.close()


async def repaired_delivery(host, name):
    prepared = await host.manage_delivery("prepare", name=name)
    checkout = Path(prepared["receipt"]["checkout"])
    (checkout / "calc.py").write_text(
        "def discount(price, fraction):\n    return price * (1-fraction)\n"
    )
    result = await host.manage_delivery(
        "verify", delivery_id=prepared["delivery_id"], checks=[CHECK]
    )
    assert result["status"] == "passed"
    return prepared


async def test_queued_apply_uses_latest_verification(isolated_workspace, monkeypatch):
    from eventide import delivery_runtime

    host = delivery_host(isolated_workspace)
    entered, release = asyncio.Event(), asyncio.Event()

    class FailedCheck:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, command, cwd):
            entered.set()
            await release.wait()
            return CommandResult(1, stderr="independent check failed")

    try:
        prepared = await repaired_delivery(host, "latest")
        monkeypatch.setattr(delivery_runtime, "LocalCommandExecutor", FailedCheck)
        verify = asyncio.create_task(
            host.manage_delivery("verify", delivery_id=prepared["delivery_id"], checks=["false"])
        )
        await asyncio.wait_for(entered.wait(), 3)
        apply = asyncio.create_task(
            host.manage_delivery("apply", delivery_id=prepared["delivery_id"])
        )
        await asyncio.sleep(0.01)
        release.set()
        assert (await verify)["status"] == "failed"
        with pytest.raises(ValueError, match="no passing"):
            await apply
        assert "price + fraction" in (host.settings.workdir / "calc.py").read_text()
    finally:
        release.set()
        await host.close()


async def test_apply_stop_records_patch_that_already_happened(isolated_workspace, monkeypatch):
    from eventide import delivery_runtime

    host = delivery_host(isolated_workspace)
    entered, release = threading.Event(), threading.Event()
    original = delivery_runtime.apply_delivery

    def held_apply(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result

    task = None
    try:
        prepared = await repaired_delivery(host, "stop-apply")
        monkeypatch.setattr(delivery_runtime, "apply_delivery", held_apply)
        task = asyncio.create_task(
            host.manage_delivery("apply", delivery_id=prepared["delivery_id"])
        )
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await host.manage_delivery("show", delivery_id=prepared["delivery_id"]))["applied"]
        assert "price * (1-fraction)" in (host.settings.workdir / "calc.py").read_text()
    finally:
        release.set()
        await host.close()


async def test_verify_stop_preserves_unknown_cleanup_without_completed(
    isolated_workspace, monkeypatch
):
    from eventide import container_executor

    host = delivery_host(isolated_workspace)
    entered = asyncio.Event()

    class UnknownContainer:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, command, cwd):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ContainerCleanupError("not confirmed stopped") from None

    try:
        prepared = await host.manage_delivery("prepare", name="stop-check")
        monkeypatch.setattr(container_executor, "ContainerCommandExecutor", UnknownContainer)
        task = asyncio.create_task(
            host.manage_delivery(
                "verify", delivery_id=prepared["delivery_id"], checks=[CHECK], image="python:local"
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session = f"deliveries_{host.default_workspace.workspace_id}"
        run = host.store.latest_run(session)
        assert run["status"] == "interrupted"
        types = [e["type"] for e in host.store.run_events(run["id"])]
        assert "tool.outcome_unknown" in types and "tool.completed" not in types
    finally:
        await host.close()
