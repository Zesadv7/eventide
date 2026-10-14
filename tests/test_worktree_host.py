"""Worktree control-plane ownership, audit and CLI integration."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from eventide import cli
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.worktrees import WorktreeService
from tests.test_host import make_repo
from tests.test_runtime import settings_for


async def test_worktree_lifecycle_is_audited_without_a_model(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state")
    host = RuntimeHost(settings, ScriptedProvider([]))
    try:
        target = host.default_workspace.workspace_id
        assert await host.manage_worktree(target, "list") == []
        assert host.store.list_sessions(target) == []
        created = await host.manage_worktree(target, "create", name="review")
        kept = await host.manage_worktree(target, "keep", name="review")
        assert kept["kept"]
        assert (await host.manage_worktree(target, "list"))[0] == kept
        removed = await host.manage_worktree(target, "remove", name="review")
        assert removed["path"] == created["path"]
        assert await host.manage_worktree(target, "list") == []
        session = host.store.list_sessions(target)[0]
        events = host.store.session_events(session["id"])
        assert [e["payload"]["action"] for e in events if e["type"] == "worktree.requested"] == [
            "create", "keep", "remove",
        ]
        assert len([e for e in events if e["type"] == "worktree.completed"]) == 3
        assert all(run["status"] == "completed" for run in host.store.list_runs(session["id"]))
        assert not any(tool["name"].startswith("worktree") for tool in host.tools)
    finally:
        await host.close()


async def test_worktree_failure_and_session_history_are_preserved(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(replace(settings_for(repo), state_dir=isolated_workspace / "state"))
    try:
        target = host.default_workspace.workspace_id
        with pytest.raises(ValueError):
            await host.manage_worktree(target, "create", name="../escape")
        management = host.store.list_sessions(target)[0]
        assert host.store.latest_run(management["id"])["status"] == "failed"
        created = await host.manage_worktree(target, "create", name="active")
        checkout = host.resolve_or_register_workspace(created["path"])
        session = host.create_session(workspace_id=checkout.workspace_id)
        host.store.update_session(session, archived=True)
        with pytest.raises(ValueError, match="Session history"):
            await host.manage_worktree(target, "remove", name="active")
        assert len(await host.manage_worktree(target, "list")) == 1
        with pytest.raises(ValueError, match="Unknown"):
            await host.manage_worktree(target, "merge", name="active")
        with pytest.raises(ValueError, match="required"):
            await host.manage_worktree(target, "create")
    finally:
        await host.close()
    with pytest.raises(ValueError, match="closed"):
        await host.manage_worktree(target, "list")


async def test_cancel_retains_git_ownership_and_records_outcome(isolated_workspace, monkeypatch):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(replace(settings_for(repo), state_dir=isolated_workspace / "state"))
    entered, released = threading.Event(), threading.Event()
    original = WorktreeService.create

    def slow_create(service, *args, **kwargs):
        entered.set()
        assert released.wait(10)
        return original(service, *args, **kwargs)

    monkeypatch.setattr(WorktreeService, "create", slow_create)
    target = host.default_workspace.workspace_id
    task = asyncio.create_task(host.manage_worktree(target, "create", name="stopped"))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(await host.manage_worktree(target, "list")) == 1
        session = host.store.list_sessions(target)[0]
        assert host.store.latest_run(session["id"])["status"] == "completed"
    finally:
        released.set()
        await host.close()


async def test_removal_closes_checkout_admission_until_git_finishes(
    isolated_workspace, monkeypatch
):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(replace(settings_for(repo), state_dir=isolated_workspace / "state"))
    target = host.default_workspace.workspace_id
    created = await host.manage_worktree(target, "create", name="reserved")
    checkout = host.resolve_or_register_workspace(created["path"])
    entered, released = threading.Event(), threading.Event()
    original = WorktreeService.remove

    def slow_remove(service, name):
        entered.set()
        assert released.wait(10)
        return original(service, name)

    monkeypatch.setattr(WorktreeService, "remove", slow_remove)
    task = asyncio.create_task(host.manage_worktree(target, "remove", name="reserved"))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        with pytest.raises(ValueError, match="being removed"):
            host.create_session(workspace_id=checkout.workspace_id)
        with pytest.raises(ValueError, match="being removed"):
            host.resolve_or_register_workspace(checkout.workspace_id)
        with pytest.raises(ValueError, match="being removed"):
            host.resolve_or_register_workspace(created["path"])
        released.set()
        assert (await task)["name"] == "reserved"
        assert not host._worktree_removals
        assert all(
            item.workspace_id != checkout.workspace_id for item in host.store.list_workspaces()
        )
    finally:
        released.set()
        await host.close()


def test_worktree_cli_uses_host_control_plane(isolated_workspace, monkeypatch, capsys):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state")
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    assert cli.main(["worktree", "--workspace", str(repo), "create", "cli-check"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["name"] == "cli-check"
    assert cli.main(["worktree", "--workspace", str(repo), "list"]) == 0
    assert json.loads(capsys.readouterr().out) == [record]
    assert cli.main(["worktree", "--workspace", str(repo), "remove", "cli-check"]) == 0
    capsys.readouterr()
    assert cli.main(["worktree", "--workspace", str(repo), "create", "../escape"]) == 1
    assert "Worktree name" in capsys.readouterr().out
