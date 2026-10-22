"""Unconfirmed process cleanup cannot become a known, resumable side effect."""

import asyncio
from dataclasses import replace

import pytest

from eventide.container_executor import ContainerCleanupError
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for


class UnknownExecutor:
    def __init__(self, blocked=False):
        self.blocked = blocked
        self.entered = asyncio.Event()

    async def execute(self, command, cwd):
        self.entered.set()
        if self.blocked:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ContainerCleanupError("Container cleanup is unconfirmed") from None
        raise ContainerCleanupError("Container cleanup is unconfirmed")


def make_host(path, executor):
    repo = make_repo(path / "repo")
    host = RuntimeHost(
        replace(settings_for(repo), state_dir=path / "state"),
        ScriptedProvider(
            [{"tool_calls": [{"id": "shell", "name": "bash", "arguments": {"command": "work"}}]}]
        ),
    )
    host.executor.command_executor = executor
    return host


async def test_unknown_result_parks_and_quarantines_workspace_across_restart(isolated_workspace):
    host = make_host(isolated_workspace, UnknownExecutor())
    try:
        session = host.create_session()
        result = await host.run(RunRequest("write", session))
        assert result.status == "interrupted"
        run = host.store.get_run(result.run_id)
        assert run["reason"] == "unknown_tool_outcome"
        assert run["tools"]["shell"]["status"] == "outcome_unknown"
        assert run["tools"]["shell"]["arguments"] == {"command": "work"}
        assert "ContainerCleanupError" in run["tools"]["shell"]["error"]
        assert not any(e["type"] == "tool.completed" for e in host.store.run_events(result.run_id))
        state, repo = host.state_dir, host.settings.workdir
        await host.close()
        host = RuntimeHost(replace(settings_for(repo), state_dir=state), ScriptedProvider([]))
        with pytest.raises(ValueError, match="unresolved"):
            await host.continue_session(session)
        with pytest.raises(ValueError, match="unresolved"):
            await host.run(RunRequest("new goal", host.create_session()))
        with pytest.raises(ValueError, match="unresolved"):
            await host.manage_delivery("prepare", name="cannot-bypass")
    finally:
        await host.close()


async def test_unknown_cleanup_during_stop_is_recorded_before_terminal(isolated_workspace):
    executor = UnknownExecutor(blocked=True)
    host = make_host(isolated_workspace, executor)
    session = host.create_session()
    task = asyncio.create_task(host.run(RunRequest("stop unknown", session)))
    try:
        await asyncio.wait_for(executor.entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = host.store.latest_run(session)
        assert result["reason"] == "unknown_tool_outcome"
        types = [e["type"] for e in host.store.run_events(result["id"])]
        assert types[-2:] == ["workspace.checkpoint", "run.interrupted"]
        assert "tool.outcome_unknown" in types
        assert "tool.completed" not in types
        assert host.store.workspace_has_unknown_outcome(host.default_workspace.workspace_id)
    finally:
        await host.close()
