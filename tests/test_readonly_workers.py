"""Workers share runtime ownership without sharing conversation or write authority."""

import asyncio
import json
from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def worker_host(path, provider, **changes):
    repo = make_repo(path / "repo")
    (repo / "code.py").write_text("value = 1\nprint(value)\n")
    return RuntimeHost(
        replace(settings_for(repo), state_dir=path / "state", workers_enabled=True, **changes),
        provider,
    )


def report(path="code.py", line=1):
    return json.dumps(
        {
            "summary": "read source",
            "findings": [
                {"path": path, "start_line": line, "end_line": line, "message": "a value"}
            ],
        }
    )


class WorkerProvider:
    def __init__(self, *, invalid=False, blocked=False):
        self.requests = []
        self.entered = asyncio.Event()
        self.both = asyncio.Event()
        self.active = 0
        self.invalid = invalid
        self.blocked = blocked

    async def complete(self, request):
        self.requests.append(request)
        worker = "You are a read-only" in request.messages[0]["content"]
        if worker:
            assert "private parent history" not in str(request.messages)
            assert {tool["name"] for tool in request.tools} == {
                "read_file",
                "glob",
                "search_text",
                "compact",
                "read_tool_result",
            }
            if len(request.messages) == 1:
                self.active += 1
                self.entered.set()
                if self.active == 2:
                    self.both.set()
                if self.blocked:
                    await asyncio.Event().wait()
                await self.both.wait()
                return ModelResponse(
                    tool_calls=(
                        ToolCall("write", "write_file", {"path": "code.py", "content": "bad"}),
                        ToolCall(
                            "recurse",
                            "delegate_readonly",
                            {"tasks": [{"role": "reviewer", "goal": "recursion"}]},
                        ),
                        ToolCall("read", "read_file", {"path": "code.py"}),
                        ToolCall("search", "search_text", {"query": "value", "pattern": "*.py"}),
                    ),
                    usage={"input_tokens": 3, "output_tokens": 2},
                )
            return ModelResponse(
                text=report(line=100 if self.invalid else 1),
                usage={"input_tokens": 4, "output_tokens": 1},
            )
        if len(request.messages) == 1:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "delegate",
                        "delegate_readonly",
                        {
                            "tasks": [
                                {"role": "researcher", "goal": "locate value"},
                                {"role": "reviewer", "goal": "review value"},
                            ]
                        },
                    ),
                ),
                usage={"input_tokens": 2, "output_tokens": 1},
            )
        return ModelResponse(text="coordinated", usage={"input_tokens": 5, "output_tokens": 2})


async def test_workers_are_concurrent_isolated_readonly_and_accounted(isolated_workspace):
    provider = WorkerProvider()
    host = worker_host(isolated_workspace, provider)
    try:
        session = host.create_session()
        result = await asyncio.wait_for(host.run(RunRequest("private parent history", session)), 5)
        assert result.status == "completed", result.output
        assert result.usage == {"input_tokens": 21, "output_tokens": 9}
        events = host.store.run_events(result.run_id)
        starts = [e["payload"] for e in events if e["type"] == "worker.started"]
        completions = [e["payload"] for e in events if e["type"] == "worker.completed"]
        assert len(starts) == len(completions) == 2
        assert all(e["report_valid"] for e in completions)
        assert len({start["child_session_id"] for start in starts}) == 2
        for start in starts:
            child = host.store.get_run(start["child_run_id"])
            assert child["parent_run_id"] == result.run_id
            assert child["usage"] == {"input_tokens": 7, "output_tokens": 3}
            tools = child["tools"]
            assert tools["write"]["is_error"] and tools["recurse"]["is_error"]
            assert "value = 1" in tools["read"]["content"]
            assert "code.py:1: value = 1" in tools["search"]["content"]
            with pytest.raises(ValueError, match="Worker sessions"):
                await host.run(RunRequest("try to write source", start["child_session_id"]))
            with pytest.raises(ValueError, match="Worker sessions"):
                await host.continue_session(start["child_session_id"])
        assert (host.settings.workdir / "code.py").read_text() == "value = 1\nprint(value)\n"
        assert not list((host.state_dir / "workers").iterdir())
        assert (
            len(
                host.store.list_sessions(
                    host.default_workspace.workspace_id, include_archived=False
                )
            )
            == 1
        )
        assert not host._active_tasks
    finally:
        await host.close()


async def test_invalid_worker_citations_are_not_adopted(isolated_workspace):
    host = worker_host(isolated_workspace, WorkerProvider(invalid=True))
    try:
        result = await asyncio.wait_for(host.run(RunRequest("review")), 5)
        delegated = host.store.get_run(result.run_id)["tools"]["delegate"]
        assert delegated["is_error"]
        assert '"report": null' in delegated["content"]
        assert "line" in delegated["content"].lower()
    finally:
        await host.close()


async def test_cancelling_parent_waits_for_children_and_records_usage(isolated_workspace):
    provider = WorkerProvider(blocked=True)
    host = worker_host(isolated_workspace, provider)
    task = asyncio.create_task(host.run(RunRequest("stop all")))
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not host._active_tasks
        assert not host._run_tasks
        sessions = host.store.list_sessions(host.default_workspace.workspace_id)
        assert len(sessions) == 3
        for session in sessions:
            assert host.store.latest_run(session["id"])["status"] == "interrupted"
        assert not list((host.state_dir / "workers").iterdir())
    finally:
        await host.close()


async def test_children_spend_parent_soft_budget_before_next_request(isolated_workspace):
    host = worker_host(isolated_workspace, WorkerProvider(), run_token_limit=10)
    try:
        result = await asyncio.wait_for(host.run(RunRequest("budget")), 5)
        assert result.status == "interrupted"
        assert host.store.get_run(result.run_id)["reason"] == "token_budget"
        assert result.usage == {"input_tokens": 8, "output_tokens": 5}
        assert (
            len(
                [
                    r
                    for r in host.provider.requests
                    if "You are a read-only" not in r.messages[0]["content"]
                ]
            )
            == 1
        )
    finally:
        await host.close()


async def test_worker_deadline_returns_explicit_error_and_cleans_children(
    isolated_workspace, monkeypatch
):
    from eventide import host as host_module

    monkeypatch.setattr(host_module, "READONLY_WORKER_TIMEOUT", 0.02)
    host = worker_host(isolated_workspace, WorkerProvider(blocked=True))
    try:
        result = await asyncio.wait_for(host.run(RunRequest("timeout")), 5)
        delegated = host.store.get_run(result.run_id)["tools"]["delegate"]
        assert delegated["is_error"]
        assert "Worker timed out" in delegated["content"]
        assert not host._active_tasks
        assert not host._run_tasks
        assert not list((host.state_dir / "workers").iterdir())
    finally:
        await host.close()
