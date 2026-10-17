"""Project identities aggregate local copies without rebinding old conversations."""

import asyncio
import sqlite3
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from tests.test_delivery_runtime import delivery_host
from tests.test_host import make_repo
from tests.test_readonly_workers import WorkerProvider, worker_host
from tests.test_runtime import settings_for


def linked_repo(root):
    source = make_repo(root / "source")
    copy = root / "copy"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "-b", "codex/copy", str(copy)],
        check=True,
        capture_output=True,
    )
    return source, copy


@pytest.mark.parametrize("copy_first", [False, True])
def test_git_siblings_share_project_and_primary_source(isolated_workspace, copy_first):
    source, copy = linked_repo(isolated_workspace)
    database = isolated_workspace / "runtime.sqlite"
    store = RuntimeStore(database)
    first, second = (copy, source) if copy_first else (source, copy)
    store.register_workspace(first, str(first))
    store.register_workspace(second, str(second))
    records = {item.path: item for item in store.list_workspaces()}
    original, execution = records[str(source)], records[str(copy)]
    assert original.project_id == execution.project_id
    assert original.source_workspace_id == execution.source_workspace_id == original.workspace_id
    assert original.kind == "source" and execution.kind == "worktree"
    store.create_session("original", original.workspace_id)
    store.create_session("execution", execution.workspace_id)
    store.update_session("execution", archived=True)
    assert {s["id"] for s in store.list_project_sessions(original.project_id)} == {
        "original",
        "execution",
    }
    assert [
        s["id"]
        for s in store.list_project_sessions(
            original.project_id,
            include_archived=False,
        )
    ] == ["original"]
    with pytest.raises(ValueError, match="different workspace"):
        store.create_session("original", execution.workspace_id)
    project = store.get_project(original.project_id)
    store.close()
    reopened = RuntimeStore(database)
    assert reopened.get_project(project.project_id) == project
    assert reopened.get_workspace(original.workspace_id) == original
    assert reopened.get_workspace(execution.workspace_id) == execution
    assert len(reopened.list_projects()) == 1
    reopened.close()


def test_independent_clones_and_non_git_paths_are_not_grouped(isolated_workspace):
    source = make_repo(isolated_workspace / "source")
    clone = isolated_workspace / "clone"
    subprocess.run(["git", "clone", str(source), str(clone)], check=True, capture_output=True)
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    records = [
        store.register_workspace(path, str(path) if path != isolated_workspace else None)
        for path in (source, clone, isolated_workspace)
    ]
    assert len({record.project_id for record in records}) == 3
    assert all(record.source_workspace_id == record.workspace_id for record in records)
    with pytest.raises(ValueError, match="Project not found"):
        store.get_project("missing")
    store.close()


def test_project_source_and_history_cannot_be_orphaned(isolated_workspace):
    source, copy = linked_repo(isolated_workspace)
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    original = store.register_workspace(source, str(source))
    execution = store.register_workspace(copy, str(copy))
    with pytest.raises(ValueError, match="execution copies"):
        store.remove_workspace(original.workspace_id)
    store.create_session("historic", execution.workspace_id)
    with pytest.raises(ValueError, match="orphan"):
        store.remove_workspace(execution.workspace_id)
    store.delete_session("historic")
    store.remove_workspace(execution.workspace_id)
    assert store.get_project(original.project_id)
    store.remove_workspace(original.workspace_id)
    assert store.list_projects() == []
    assert source.is_dir() and copy.is_dir()
    store.close()


def test_schema_three_migration_preserves_ids_binding_and_event_bytes(isolated_workspace):
    source, copy = linked_repo(isolated_workspace)
    database = isolated_workspace / "legacy.sqlite"
    store = RuntimeStore(database)
    original = store.register_workspace(source, str(source))
    execution = store.register_workspace(copy, str(copy))
    store.create_session("old-session", execution.workspace_id, working_directory=".")
    store.create_run("old-run", "old-session")
    store.append_event("old-run", "run.started", {"workspace_id": execution.workspace_id})
    store.finish_run("old-run", status="completed", output="old history")
    event_bytes = store._connection.execute("SELECT * FROM runtime_events").fetchall()
    old_session = store.get_session("old-session")
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE workspace_projects")
        connection.execute("DROP TABLE projects")
        connection.execute("DELETE FROM schema_migrations WHERE version>=4")
    migrated = RuntimeStore(database)
    assert migrated.get_session("old-session") == old_session
    assert (
        migrated.get_workspace(original.workspace_id).project_id
        == migrated.get_workspace(
            execution.workspace_id,
        ).project_id
    )
    assert [tuple(row) for row in migrated._connection.execute("SELECT * FROM runtime_events")] == [
        tuple(row) for row in event_bytes
    ]
    assert "project_id" not in migrated.run_events("old-run")[0]["payload"]
    assert migrated.get_run("old-run")["output"] == "old history"
    migrated.close()


def test_missing_historical_path_migrates_without_rebinding(isolated_workspace):
    database = isolated_workspace / "old.sqlite"
    store = RuntimeStore(database)
    missing = store.register_workspace(isolated_workspace / "gone", "/gone")
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE workspace_projects")
        connection.execute("DROP TABLE projects")
        connection.execute("DELETE FROM schema_migrations WHERE version>=4")
    migrated = RuntimeStore(database)
    saved = migrated.get_workspace(missing.workspace_id)
    assert saved.path == missing.path and saved.kind == "source"
    assert migrated.get_project(saved.project_id).git_common_dir is None
    migrated.close()


def test_registration_is_idempotent_across_database_connections(isolated_workspace):
    source, copy = linked_repo(isolated_workspace)
    database = isolated_workspace / "runtime.sqlite"
    stores = [RuntimeStore(database), RuntimeStore(database)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: pair[0].register_workspace(pair[1], str(pair[1])),
                    zip(stores, (source, copy), strict=True),
                )
            )
        assert results[0].project_id == results[1].project_id
        assert len(stores[0].list_workspaces()) == 2
        assert len(stores[0].list_projects()) == 1
    finally:
        for store in stores:
            store.close()


def test_git_environment_cannot_redirect_project_identity(isolated_workspace, monkeypatch):
    source, copy = linked_repo(isolated_workspace)
    unrelated = make_repo(isolated_workspace / "unrelated")
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    settings = replace(settings_for(copy), state_dir=isolated_workspace / "state")
    host = RuntimeHost(settings, ScriptedProvider([]))
    try:
        workspace = host.default_workspace
        assert workspace.path == str(copy)
        assert host.store.get_workspace(workspace.source_workspace_id).path == str(source)
    finally:
        asyncio.run(host.close())


def test_project_api_aggregates_and_keeps_workspace_contract(isolated_workspace):
    source, copy = linked_repo(isolated_workspace)
    host = RuntimeHost(replace(settings_for(source), state_dir=isolated_workspace / "state"))
    with TestClient(create_app(host)) as client:
        result = client.post(
            "/api/workspaces",
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"path": str(copy)},
        )
        assert result.status_code == 201
        execution = result.json()
        project_id = execution["project_id"]
        assert execution["kind"] == "worktree"
        assert (
            client.post(
                "/api/workspaces",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={"path": str(copy)},
            ).json()
            == execution
        )
        sessions = [
            client.post(
                "/api/sessions",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={"workspace_id": workspace_id},
            ).json()["session_id"]
            for workspace_id in (execution["source_workspace_id"], execution["workspace_id"])
        ]
        assert {
            s["session_id"] for s in client.get(f"/api/projects/{project_id}/sessions").json()
        } == set(sessions)
        assert client.get(f"/api/sessions/{sessions[1]}").json()["project_id"] == project_id
        client.patch(
            f"/api/sessions/{sessions[1]}",
            json={
                "archived": True,
                "expected_metadata_hash": client.get(f"/api/sessions/{sessions[1]}").json()[
                    "metadata_hash"
                ],
            },
            headers={"Idempotency-Key": "archive"},
        )
        assert len(client.get(f"/api/projects/{project_id}/sessions").json()) == 1
        assert (
            len(
                [
                    s
                    for s in client.get(
                        f"/api/projects/{project_id}/sessions?include_archived=true"
                    ).json()
                    if not s["managed"]
                ]
            )
            == 2
        )
        assert client.get(f"/api/projects/{project_id}").json()["workspace_count"] == 2
        assert client.get(f"/api/projects/{project_id}").json()["session_count"] == 3
        assert len(client.get("/api/projects").json()) == 1
        assert len(client.get(f"/api/projects/{project_id}/workspaces").json()) == 2
        for path in ("", "/sessions", "/workspaces"):
            assert client.get(f"/api/projects/missing{path}").status_code == 404
        assert (
            client.delete(
                f"/api/workspaces/{execution['source_workspace_id']}",
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ).status_code
            == 409
        )


@pytest.mark.parametrize("admitted", [False, True])
async def test_run_facts_freeze_authoritative_project(isolated_workspace, admitted):
    source, copy = linked_repo(isolated_workspace)
    host = RuntimeHost(
        replace(settings_for(source), state_dir=isolated_workspace / "state"),
        ScriptedProvider([{"text": "done"}]),
    )
    try:
        workspace = host.resolve_or_register_workspace(copy)
        session = host.create_session(workspace_id=workspace.workspace_id)
        request = RunRequest("hello", session_id=session)
        if admitted:
            run_id = "run-project"
            await host.admit_run(replace(request, run_id=run_id))
            await host.start_admitted_run(run_id)
        else:
            run_id = (await host.run(request)).run_id
        identities = [
            e["payload"]
            for e in host.store.run_events(run_id)
            if e["type"] in {"run.started", "run.admitted"}
        ]
        assert len(identities) == 2
        assert all(item["project_id"] == host.default_workspace.project_id for item in identities)
        assert all(
            item["source_workspace_id"] == host.default_workspace.workspace_id
            for item in identities
        )
        assert host.store.get_session(session)["workspace_id"] == workspace.workspace_id
    finally:
        await host.close()


async def test_managed_copy_and_delivery_writer_keep_source_project(isolated_workspace):
    host = delivery_host(isolated_workspace, [{"text": "done"}])
    try:
        managed = await host.manage_worktree(
            host.default_workspace.workspace_id, "create", name="p1"
        )
        registered = host.resolve_or_register_workspace(managed["path"])
        assert registered.project_id == host.default_workspace.project_id
        prepared = await host.manage_delivery("prepare", name="writer")
        writer = await host.manage_delivery(
            "run", delivery_id=prepared["delivery_id"], goal="inspect"
        )
        facts = host.store.run_events(writer["worker_run_id"])
        admitted = next(e["payload"] for e in facts if e["type"] == "run.admitted")
        assert admitted["project_id"] == host.default_workspace.project_id
        assert admitted["workspace_id"] != admitted["source_workspace_id"]
        assert admitted["execution_config"]["writer_delivery_id"] == prepared["delivery_id"]
    finally:
        await host.close()


async def test_readonly_children_keep_project_without_rebinding_to_temporary_snapshot(
    isolated_workspace,
):
    host = worker_host(isolated_workspace, WorkerProvider())
    try:
        result = await host.run(RunRequest("private parent history"))
        assert result.status == "completed"
        children = [
            event["payload"]
            for event in host.store.facts_by_type(("run.admitted",))
            if event["payload"].get("parent_run_id") == result.run_id
        ]
        assert len(children) == 2
        for child in children:
            assert child["project_id"] == host.default_workspace.project_id
            assert child["snapshot_id"]
            assert (
                host.store.get_session(child["session_id"])["workspace_id"]
                == host.default_workspace.workspace_id
            )
        assert len(host.store.list_workspaces()) == 1
    finally:
        await host.close()


async def test_continue_after_restart_preserves_project_and_session_copy(isolated_workspace):
    source, copy = linked_repo(isolated_workspace)
    settings = replace(settings_for(copy), state_dir=isolated_workspace / "state", max_steps=1)
    host = RuntimeHost(
        settings,
        ScriptedProvider(
            [
                {"tool_calls": [{"id": "files", "name": "glob", "arguments": {"pattern": "*"}}]},
            ]
        ),
    )
    session = host.create_session()
    workspace = host.default_workspace
    result = await host.run(RunRequest("inspect", session, mode="plan"))
    assert result.status == "interrupted"
    await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([{"text": "done"}]))
    try:
        resumed = await reopened.continue_session(session)
        assert resumed.status == "completed"
        start = next(
            event["payload"]
            for event in reopened.store.run_events(resumed.run_id)
            if event["type"] == "run.started"
        )
        assert start["project_id"] == workspace.project_id
        assert start["source_workspace_id"] == workspace.source_workspace_id
        assert start["workspace_id"] == workspace.workspace_id
        assert start["continuation_of"] == result.run_id
        assert reopened.store.get_workspace(workspace.source_workspace_id).path == str(source)
    finally:
        await reopened.close()


def test_existing_plain_project_can_gain_git_copies_without_rebinding(isolated_workspace):
    source = isolated_workspace / "source"
    source.mkdir()
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    plain = store.register_workspace(source, None)
    store.create_session("before-git", plain.workspace_id)
    subprocess.run(["git", "-C", str(source), "init"], check=True, capture_output=True)
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
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    copy = isolated_workspace / "copy"
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "worktree",
            "add",
            "-b",
            "codex/added",
            str(copy),
        ],
        check=True,
        capture_output=True,
    )
    execution = store.register_workspace(copy, str(copy))
    assert execution.project_id == plain.project_id
    assert execution.source_workspace_id == plain.workspace_id
    assert store.get_session("before-git")["workspace_id"] == plain.workspace_id
    assert store.get_project(plain.project_id).git_common_dir == str(source / ".git")
    store.close()
