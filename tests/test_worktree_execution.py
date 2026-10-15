"""Writable conversation copies preserve history without changing Session identity."""

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def make_host(root, provider, **options):
    source = make_repo(root / "source")
    (source / "code.py").write_text("original\n")
    subprocess.run(["git", "-C", str(source), "add", "code.py"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "add code"], check=True)
    return RuntimeHost(replace(settings_for(source), state_dir=root / "state", **options), provider)


def request(session=None, **kwargs):
    return RunRequest("edit", session_id=session, execution_target="worktree", **kwargs)


class Writers:
    def __init__(self):
        self.entered = 0
        self.both = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, query):
        if len(query.messages) == 1:
            self.entered += 1
            number = self.entered
            if number == 2:
                self.both.set()
            await self.release.wait()
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write_file",
                        {
                            "path": "code.py",
                            "content": f"writer {number}\n",
                        },
                    ),
                )
            )
        return ModelResponse(text="done")


async def test_two_sessions_write_isolated_copies_and_free_source(isolated_workspace):
    provider = Writers()
    host = make_host(isolated_workspace, provider)
    sessions = [host.create_session(), host.create_session()]
    tasks = [asyncio.create_task(host.run(request(session))) for session in sessions]
    try:
        await asyncio.wait_for(provider.both.wait(), 8)
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        provider.release.set()
        results = await asyncio.gather(*tasks)
        assert all(result.status == "completed" for result in results)
        bindings = [host.run_execution(result.run_id) for result in results]
        copies = [host.store.get_workspace(item["execution_workspace_id"]) for item in bindings]
        assert copies[0].workspace_id != copies[1].workspace_id
        assert {Path(copy.path, "code.py").read_text() for copy in copies} == {
            "writer 1\n",
            "writer 2\n",
        }
        assert (host.settings.workdir / "code.py").read_text() == "original\n"
        assert all(copy.project_id == host.default_workspace.project_id for copy in copies)
        assert all(
            host.store.get_session(session)["workspace_id"] == host.default_workspace.workspace_id
            for session in sessions
        )
    finally:
        provider.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()


async def test_followup_and_restart_reuse_same_dirty_copy(isolated_workspace):
    host = make_host(
        isolated_workspace,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "write",
                            "name": "write_file",
                            "arguments": {
                                "path": "code.py",
                                "content": "kept edit\n",
                            },
                        }
                    ]
                },
                {"text": "first done"},
            ]
        ),
    )
    session = host.create_session()
    result = await host.run(request(session))
    assert result.status == "completed"
    binding = host.run_execution(result.run_id)
    settings = host.settings
    await host.close()
    (settings.workdir / "code.py").write_text("source drift after first run\n")
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}]},
            {"text": "second done"},
        ]
    )
    host = RuntimeHost(settings, provider)
    try:
        followup = await host.run(request(session))
        assert followup.status == "completed"
        assert host.run_execution(followup.run_id) == binding
        assert "kept edit" in str(provider.requests[1].messages[-1])
        with pytest.raises(ValueError, match="Session owns a worktree"):
            await host.run(RunRequest("accidentally source", session))
        copy = host.store.get_workspace(binding["execution_workspace_id"])
        with pytest.raises(ValueError, match="orphan history"):
            host.store.remove_workspace(copy.workspace_id)
        with pytest.raises(ValueError, match="history"):
            await host.manage_worktree(
                host.default_workspace.workspace_id, "remove", name=binding["name"]
            )
    finally:
        await host.close()


async def test_worktree_continue_preserves_copy_and_rejects_copy_drift(isolated_workspace):
    host = make_host(
        isolated_workspace,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "write",
                            "name": "write_file",
                            "arguments": {
                                "path": "code.py",
                                "content": "first edit\n",
                            },
                        }
                    ]
                },
                {"text": "continued"},
            ]
        ),
        max_steps=1,
    )
    try:
        first = await host.run(request())
        assert first.status == "interrupted"
        binding = host.run_execution(first.run_id)
        (host.settings.workdir / "code.py").write_text("unrelated source change\n")
        second = await host.continue_session(first.session_id)
        assert second.status == "completed"
        assert host.run_execution(second.run_id) == binding
    finally:
        await host.close()


async def test_worktree_initial_dirty_source_refused_without_creating_copy(isolated_workspace):
    host = make_host(isolated_workspace, ScriptedProvider([]))
    (host.settings.workdir / "code.py").write_text("user dirty work\n")
    try:
        with pytest.raises(ValueError, match="clean"):
            await host.admit_run(request())
        assert await host.manage_worktree(host.default_workspace.workspace_id, "list") == []
    finally:
        await host.close()


async def test_unknown_copy_cannot_be_bypassed_after_abandon_and_does_not_block_source(
    isolated_workspace,
):
    from tests.test_unknown_outcome import UnknownExecutor

    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "shell", "name": "bash", "arguments": {"command": "work"}}]},
            {"text": "source available"},
            {"text": "other copy available"},
        ]
    )
    host = make_host(isolated_workspace, provider)
    host.executor.command_executor = UnknownExecutor()
    try:
        result = await host.run(request())
        assert result.status == "interrupted"
        binding = host.run_execution(result.run_id)
        assert host.store.workspace_has_unknown_outcome(binding["execution_workspace_id"])
        assert not host.store.workspace_has_unknown_outcome(host.default_workspace.workspace_id)
        with pytest.raises(ValueError, match="Unknown tool outcome|unresolved"):
            await host.continue_session(result.session_id)
        await host.abandon_interruption(result.session_id)
        assert host.session_status(result.session_id)["execution"] == binding
        with pytest.raises(ValueError, match="unresolved"):
            await host.run(request(result.session_id))
        assert (await host.run(RunRequest("source run"))).status == "completed"
        assert (await host.run(request())).status == "completed"
    finally:
        await host.close()


async def test_copy_drift_after_acceptance_or_park_is_refused(isolated_workspace):
    host = make_host(isolated_workspace, ScriptedProvider([]))
    try:
        run_id = await host.admit_run(request())
        binding = host.run_execution(run_id)
        copy = host.store.get_workspace(binding["execution_workspace_id"])
        Path(copy.path, "code.py").write_text("changed before execution")
        with pytest.raises(ValueError, match="changed after admission"):
            await host.start_admitted_run(run_id)
        assert host.provider.requests == []
        session = host.create_session()
        parked = await host.admit_run(request(session))
        await host.cancel_run(parked)
        copy = host.store.get_workspace(host.run_execution(parked)["execution_workspace_id"])
        Path(copy.path, "code.py").write_text("changed after parking")
        with pytest.raises(ValueError, match="changed or checkpoint"):
            await host.continue_session(session)
    finally:
        await host.close()


async def test_stop_copy_leaves_other_writer_and_resume_bound(isolated_workspace):
    provider = Writers()
    host = make_host(isolated_workspace, provider)
    sessions = [host.create_session(), host.create_session()]
    tasks = [
        asyncio.create_task(host.run(request(session, run_id=f"writer-{number}")))
        for number, session in enumerate(sessions)
    ]
    try:
        await asyncio.wait_for(provider.both.wait(), 8)
        first_binding = host.run_execution("writer-0")
        stopped = await host.cancel_run("writer-0")
        assert stopped["status"] == "interrupted"
        assert not tasks[1].done()
        provider.release.set()
        assert (await tasks[1]).status == "completed"
        continued = await host.continue_session(sessions[0])
        assert continued.status == "completed"
        assert host.run_execution(continued.run_id) == first_binding
        assert (host.settings.workdir / "code.py").read_text() == "original\n"
    finally:
        provider.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()


async def test_shared_git_commands_serialize_without_serializing_model_calls(isolated_workspace):
    from eventide.models import CommandResult

    class ShellWriters(Writers):
        async def complete(self, query):
            if len(query.messages) == 1:
                self.entered += 1
                if self.entered == 2:
                    self.both.set()
                await self.release.wait()
                return ModelResponse(tool_calls=(ToolCall("shell", "bash", {"command": "check"}),))
            return ModelResponse(text="done")

    class Commands:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.active = 0
            self.maximum = 0
            self.paths = []

        async def execute(self, command, cwd):
            self.paths.append(cwd)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.entered.set()
            await self.release.wait()
            self.active -= 1
            return CommandResult(0, stdout="checked")

    provider, commands = ShellWriters(), Commands()
    host = make_host(isolated_workspace, provider)
    host.executor.command_executor = commands
    tasks = [asyncio.create_task(host.run(request())) for _ in range(2)]
    try:
        await asyncio.wait_for(provider.both.wait(), 8)
        provider.release.set()
        await asyncio.wait_for(commands.entered.wait(), 3)
        await asyncio.sleep(0.03)
        assert len(commands.paths) == 1
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        commands.release.set()
        assert all(result.status == "completed" for result in await asyncio.gather(*tasks))
        assert commands.maximum == 1
        assert len(set(commands.paths)) == 2
    finally:
        provider.release.set()
        commands.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()


async def test_worktree_workers_see_copy_and_share_project_budget_identity(isolated_workspace):
    from tests.test_readonly_workers import report

    class Provider:
        def __init__(self):
            self.parent_calls = 0

        async def complete(self, query):
            if "You are a read-only" in query.messages[0]["content"]:
                if len(query.messages) == 1:
                    return ModelResponse(
                        tool_calls=(ToolCall("read", "read_file", {"path": "code.py"}),)
                    )
                return ModelResponse(text=report())
            self.parent_calls += 1
            if self.parent_calls == 1:
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "write",
                            "write_file",
                            {
                                "path": "code.py",
                                "content": "copy-only change\n",
                            },
                        ),
                    )
                )
            if self.parent_calls == 2:
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "delegate",
                            "delegate_readonly",
                            {
                                "tasks": [{"role": "reviewer", "goal": "review changed file"}],
                            },
                        ),
                    )
                )
            return ModelResponse(text="done")

    host = make_host(isolated_workspace, Provider(), workers_enabled=True)
    try:
        result = await host.run(request())
        assert result.status == "completed", result.output
        child = next(
            event["payload"]
            for event in host.store.run_events(result.run_id)
            if event["type"] == "worker.started"
        )
        run = host.store.get_run(child["child_run_id"])
        assert "copy-only change" in run["tools"]["read"]["content"]
        child_workspace = host.sessions.workspace(child["child_session_id"])
        assert (
            child_workspace.workspace_id
            == host.run_execution(result.run_id)["execution_workspace_id"]
        )
        assert child_workspace.project_id == host.default_workspace.project_id
        assert (host.settings.workdir / "code.py").read_text() == "original\n"
    finally:
        await host.close()


@pytest.mark.parametrize("copy_drift", [False, True])
async def test_process_exit_recovers_worktree_owner_without_replaying_or_using_source(
    isolated_workspace, copy_drift,
):
    import json
    import sys

    seed = make_host(isolated_workspace, ScriptedProvider([]))
    settings = seed.settings
    await seed.close()
    code = """
import asyncio, json, os, sys
from pathlib import Path
from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.models import RunRequest
async def main():
    host = RuntimeHost(Settings(Path(sys.argv[1]), Path(sys.argv[2]),
        'scripted', None, None, 'saved-model'), enable_mcp=False)
    session = host.create_session()
    run = await host.admit_run(RunRequest('edit', session, execution_target='worktree'))
    binding = host.run_execution(run)
    copy = host.store.get_workspace(binding['execution_workspace_id'])
    print(json.dumps({'session': session, 'run': run, 'copy': copy.path}), flush=True)
    os._exit(0)
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(settings.workdir), str(settings.state_dir)],
        capture_output=True, text=True, timeout=15,
    )
    assert child.returncode == 0, child.stderr
    saved = json.loads(child.stdout)
    (settings.workdir / "code.py").write_text("source changed after crash\n")
    if copy_drift:
        Path(saved["copy"], "code.py").write_text("unaccounted copy edit\n")
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}]},
        {"text": "recovered"},
    ])
    host = RuntimeHost(settings, provider)
    try:
        assert provider.requests == []
        assert host.store.get_run(saved["run"])["status"] == "interrupted"
        assert not host.session_busy(saved["session"])
        if copy_drift:
            with pytest.raises(ValueError, match="changed or checkpoint"):
                await host.continue_session(saved["session"])
            assert provider.requests == []
        else:
            continued = await host.continue_session(saved["session"])
            assert continued.status == "completed"
            assert "original" in str(provider.requests[1].messages)
            assert "source changed after crash" not in str(provider.requests[1].messages)
            assert host.run_execution(continued.run_id) == host.run_execution(saved["run"])
    finally:
        await host.close()


async def test_http_worktree_binding_replayed_and_source_switch_rejected(isolated_workspace):
    import httpx

    from eventide.api import create_app

    host = make_host(isolated_workspace, ScriptedProvider([{"text": "first"}, {"text": "second"}]))
    session = host.create_session()
    body = {"prompt": "edit", "execution_target": "worktree"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(host)), base_url="http://localhost",
        ) as client:
            first = await client.post(
                f"/api/sessions/{session}/runs", json=body,
                headers={"Idempotency-Key": "writer-first"},
            )
            assert first.status_code == 202
            await asyncio.gather(*list(host._run_tasks.values()))
            repeated = await client.post(
                f"/api/sessions/{session}/runs", json=body,
                headers={"Idempotency-Key": "writer-first"},
            )
            assert repeated.json()["run_id"] == first.json()["run_id"]
            run = (await client.get(f"/api/runs/{first.json()['run_id']}")).json()
            status = (await client.get(f"/api/sessions/{session}")).json()
            assert status["execution"] == run["execution"]
            assert run["execution"]["kind"] == "worktree"
            assert status["workspace_id"] == host.default_workspace.workspace_id
            rejected = await client.post(
                f"/api/sessions/{session}/runs", json={"prompt": "source by default"},
            )
            assert rejected.status_code == 422
            followup = await client.post(f"/api/sessions/{session}/runs", json=body)
            assert followup.status_code == 202
            await asyncio.gather(*list(host._run_tasks.values()))
            assert host.run_execution(followup.json()["run_id"]) == run["execution"]
    finally:
        await host.close()
