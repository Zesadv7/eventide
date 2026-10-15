"""Stopping workers preserves owner lifetime, scope, and durable read-only roles."""

import asyncio
import threading

import pytest

from eventide.host import RuntimeHost
from eventide.mcp.client import MCPManager
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.store import RuntimeStore
from eventide.tools.runtime_catalog import HANDLERS, TOOLS
from eventide.workers import SourceSnapshot
from tests.test_readonly_workers import report, worker_host


class ControlledProvider:
    def __init__(self, malicious=False):
        self.release = asyncio.Event()
        self.both = asyncio.Event()
        self.count = 0
        self.malicious = malicious

    async def complete(self, request):
        worker = "You are a read-only" in request.messages[0]["content"]
        if worker:
            assert {tool["name"] for tool in request.tools} == {
                "read_file",
                "glob",
                "compact",
                "read_tool_result",
            }
            if len(request.messages) == 1:
                self.count += 1
                if self.count == 2:
                    self.both.set()
                if self.malicious:
                    return ModelResponse(
                        tool_calls=(
                            ToolCall("custom", "evil", {}),
                            ToolCall("mcp", "mcp__fixture__risk", {}),
                            ToolCall("read", "read_file", {"path": "code.py"}),
                        ),
                        usage={"input_tokens": 1, "output_tokens": 1},
                    )
                await self.release.wait()
            return ModelResponse(text=report(), usage={"input_tokens": 1, "output_tokens": 1})
        if len(request.messages) == 1:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "delegate",
                        "delegate_readonly",
                        {
                            "tasks": [
                                {"role": "researcher", "goal": "read"},
                                {"role": "reviewer", "goal": "review"},
                            ]
                        },
                    ),
                ),
                usage={"input_tokens": 1, "output_tokens": 1},
            )
        return ModelResponse(text="parent completed", usage={"input_tokens": 1, "output_tokens": 1})


async def test_single_child_stop_does_not_cancel_parent_or_sibling(isolated_workspace):
    provider = ControlledProvider()
    host = worker_host(isolated_workspace, provider)
    task = asyncio.create_task(host.run(RunRequest("parent")))
    try:
        await asyncio.wait_for(provider.both.wait(), 3)
        children = [rid for rid in host._run_tasks if host.store.get_run(rid).get("parent_run_id")]
        assert len(children) == 2
        stopped = await asyncio.wait_for(host.cancel_run(children[0]), 2)
        assert stopped["status"] == "interrupted"
        provider.release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.status == "completed"
        assert host.store.get_run(children[1])["status"] == "completed"
        assert host.store.get_run(result.run_id)["tools"]["delegate"]["is_error"]
        assert not host._active_tasks and not host._run_tasks
        assert not list((host.state_dir / "workers").iterdir())
    finally:
        provider.release.set()
        await host.close()


async def test_host_close_waits_for_children_and_keeps_terminals(isolated_workspace):
    provider = ControlledProvider()
    host = worker_host(isolated_workspace, provider)
    state = host.state_dir
    task = asyncio.create_task(host.run(RunRequest("parent")))
    try:
        await asyncio.wait_for(provider.both.wait(), 3)
        await asyncio.wait_for(host.close(), 3)
    finally:
        await host.close()
    assert task.cancelled()
    assert not host._active_tasks and not host._run_tasks
    assert not list((state / "workers").iterdir())
    store = RuntimeStore(state / "runtime.sqlite")
    try:
        assert all(
            store.latest_run(session["id"])["status"] == "interrupted"
            for session in store.list_sessions(host.default_workspace.workspace_id)
        )
    finally:
        store.close()


@pytest.mark.parametrize("close", [False, True])
async def test_snapshot_thread_cancel_retains_owner_until_cleanup(
    isolated_workspace, monkeypatch, close
):
    entered, release = threading.Event(), threading.Event()
    original = SourceSnapshot.create

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(SourceSnapshot, "create", delayed)
    provider = ControlledProvider()
    host = worker_host(isolated_workspace, provider)
    task = asyncio.create_task(host.run(RunRequest("parent")))
    stopper = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if close:
            stopper = asyncio.create_task(host.close())
        else:
            task.cancel()
        await asyncio.sleep(0.04)
        assert not task.done()
        assert host.workspace_busy(host.default_workspace.workspace_id)
        if stopper is not None:
            assert not stopper.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        if stopper is not None:
            await asyncio.wait_for(stopper, 3)
        assert not host._active_tasks and not host._run_tasks
        assert not list((host.state_dir / "workers").iterdir())
    finally:
        release.set()
        await host.close()


async def test_custom_and_mcp_stay_disabled_without_inherited_agent_approval(
    isolated_workspace, monkeypatch
):
    provider = ControlledProvider(malicious=True)
    host = worker_host(isolated_workspace, provider)
    called = []
    connections = []

    def forbidden(**kwargs):
        called.append(kwargs)
        return "custom was used"

    async def connect(self, configs, **kwargs):
        connections.append(configs)
        return []

    monkeypatch.setattr(MCPManager, "connect_all", connect)
    host.tools = [
        *TOOLS,
        {"name": "evil", "description": "custom", "input_schema": {"type": "object"}},
    ]
    host.handlers = {**HANDLERS, "evil": forbidden, "read_file": forbidden}
    try:
        result = await asyncio.wait_for(host.run(RunRequest("parent", mode="agent")), 3)
        assert result.status == "completed"
        assert len(connections) == 1
        assert not called
        starts = [
            e["payload"]
            for e in host.store.run_events(result.run_id)
            if e["type"] == "worker.started"
        ]
        for start in starts:
            child = host.store.get_run(start["child_run_id"])
            assert child["tools"]["custom"]["is_error"]
            assert child["tools"]["mcp"]["is_error"]
            assert "value = 1" in child["tools"]["read"]["content"]
            approvals = [
                e["payload"]
                for e in host.store.run_events(child["id"])
                if e["type"] == "approval.resolved"
            ]
            # Unregistered worker tools are rejected before policy/approval.
            assert approvals == []
            assert "Unknown tool" in child["tools"]["custom"]["content"]
            assert "Unknown tool" in child["tools"]["mcp"]["content"]
    finally:
        await host.close()


async def test_worker_session_remains_restricted_after_host_restart(isolated_workspace):
    provider = ControlledProvider()
    provider.release.set()
    host = worker_host(isolated_workspace, provider)
    settings = host.settings
    try:
        result = await asyncio.wait_for(host.run(RunRequest("parent")), 3)
        starts = [
            e["payload"]
            for e in host.store.run_events(result.run_id)
            if e["type"] == "worker.started"
        ]
    finally:
        await host.close()
    restored = RuntimeHost(settings, ControlledProvider())
    try:
        for start in starts:
            session = start["child_session_id"]
            assert restored.is_worker_session(session)
            with pytest.raises(ValueError, match="Worker sessions"):
                await restored.run(RunRequest("write now", session, mode="agent"))
            with pytest.raises(ValueError, match="Worker sessions"):
                await restored.continue_session(session)
            with pytest.raises(ValueError, match="Worker sessions"):
                await restored.admit_run(RunRequest("write now", session))
    finally:
        await restored.close()
