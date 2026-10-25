"""MK-00 state-copy migration: repeatable, preserving identity, facts and artifacts.

Complements the existing schema-v6 migration tests (tests/test_file_operations.py)
with a full state-copy upgrade rehearsal: a WAL-consistent copy of a populated
state root must survive re-initialization with byte-identical history, stable
identities, memory and usage facts, and a decryptable encrypted undo artifact.
"""

import json
import shutil
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eventide.file_operations import FileOperations
from eventide.host import RuntimeHost
from eventide.preference_memory import PreferenceMemory
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from tests.test_host import make_repo
from tests.test_runtime import settings_for

MEMORY_RECORD = {
    "key": "response_style",
    "scope": "global",
    "content": "回复保持简短并给出文件行号",
}


def populated_host(path: Path):
    repo = make_repo(path / "project")
    host = RuntimeHost(
        replace(settings_for(repo), state_dir=path / "state"),
        ScriptedProvider([]),
        enable_mcp=False,
    )
    session = host.create_session()
    run_id = "run_state_copy"
    host.store.create_run(run_id, session)
    archived = host.create_session("identity-archived")
    host.store.update_session(archived, title="历史身份", archived=True)
    host.store.append_event(run_id, "message.user", {"message": {"content": "历史调查请求"}})
    host.store.append_event(run_id, "model.response", {"step": 1})
    host.store.append_event(
        run_id,
        "model.attempt_started",
        {"attempt_id": "attempt-1", "kind": "agent", "provider": "scripted", "model": "scripted"},
    )
    host.store.append_event(
        run_id,
        "model.attempt_finished",
        {
            "attempt_id": "attempt-1",
            "kind": "agent",
            "status": "completed",
            "usage": {"input": 10, "output": 5},
            "usage_source": "reported",
        },
    )
    host.memory.mutate("create", dict(MEMORY_RECORD))
    host.memory.snapshot(run_id)
    host.store.finish_run(run_id, status="completed", output="done")
    result = host.file_operations.write(
        "write_file",
        {"path": "notes/undo-me.txt", "content": "可撤销的修改内容\n"},
        run_id=run_id,
        call_id="call-1",
        workspace_id=host.default_workspace.workspace_id,
        cwd=host.settings.workdir,
    )
    operation_id = result.metadata["file_operation_id"]
    return host, session, run_id, operation_id


def copy_state_dir(state_dir: Path, target: Path) -> Path:
    """A WAL-consistent state copy: consistent DB snapshot plus sidecar files."""
    target.mkdir(parents=True, exist_ok=False)
    for item in state_dir.iterdir():
        if item.name == "runtime.sqlite" or item.name.startswith("runtime.sqlite-"):
            continue
        if item.is_dir():
            shutil.copytree(item, target / item.name)
        else:
            shutil.copy2(item, target / item.name)
    with sqlite3.connect(state_dir / "runtime.sqlite") as source, sqlite3.connect(
        target / "runtime.sqlite"
    ) as copy:
        source.backup(copy)
    return target


def snapshot(store: RuntimeStore, session: str, run_id: str, workspace_id: str) -> dict:
    return {
        "workspaces": [asdict(w) for w in store.list_workspaces()],
        "projects": [asdict(p) for p in store.list_projects()],
        "sessions": sorted(
            store.list_sessions(workspace_id, include_archived=True),
            key=lambda item: item["id"],
        ),
        "events": store.session_events(session),
        "run_events": store.run_events(run_id),
        "memory": store.facts_by_type(("memory.record",)),
        "usage": store.usage_events(run_id=run_id),
        "migrations": [
            row[0]
            for row in store._connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ],
    }


def snapshot_arguments(session: str, run_id: str, host: RuntimeHost) -> tuple[str, str, str]:
    return session, run_id, host.default_workspace.workspace_id


async def test_state_copy_reinitialization_preserves_everything_without_duplicates(
    isolated_workspace,
):
    host, session, run_id, operation_id = populated_host(isolated_workspace)
    workspace_id = host.default_workspace.workspace_id
    expected = snapshot(host.store, session, run_id, workspace_id)
    backup = host.store.file_operation_backup(operation_id)
    await host.close()

    copy_dir = copy_state_dir(isolated_workspace / "state", isolated_workspace / "state-copy")
    first = RuntimeStore(copy_dir / "runtime.sqlite")
    try:
        assert snapshot(first, session, run_id, workspace_id) == expected
        assert snapshot(first, session, run_id, workspace_id)["migrations"] == [
            1,
            2,
            3,
            4,
            5,
            6,
        ]
        # The encrypted undo artifact is byte-identical and decryptable with the
        # copied installation key; the copy never falls back to the original key.
        restored_memory = PreferenceMemory(SimpleNamespace(store=first))
        assert restored_memory.state()["memories"][0]["content"] == MEMORY_RECORD["content"]
        context, refs = restored_memory.context(run_id)
        assert MEMORY_RECORD["content"] in context
        assert len(refs) == 1
        assert first.file_operation_backup(operation_id) == backup
        restored = json.loads(FileOperations(first, copy_dir).box.decrypt(backup))
        assert restored["after"] == "可撤销的修改内容\n"
        assert restored["before"] == ""
    finally:
        first.close()

    # Repeated initialization of the same copy must not duplicate anything.
    again = RuntimeStore(copy_dir / "runtime.sqlite")
    try:
        assert snapshot(again, session, run_id, workspace_id) == expected
        # The append-only journal keeps both prepared and completed facts for the
        # single operation; a repeat initialization must not add a third one.
        operation_facts = again.facts_by_type(("file.operation",))
        assert len(operation_facts) == 2
        assert {fact["payload"]["operation_id"] for fact in operation_facts} == {operation_id}
        assert again.file_operation_backup(operation_id) == backup
    finally:
        again.close()


async def test_pre_v6_state_copy_migrates_and_keeps_history_without_backups(
    isolated_workspace,
):
    host, session, run_id, operation_id = populated_host(isolated_workspace)
    workspace_id = host.default_workspace.workspace_id
    expected = snapshot(host.store, session, run_id, workspace_id)
    await host.close()

    copy_dir = copy_state_dir(isolated_workspace / "state", isolated_workspace / "state-v5")
    # A copy saved by a v5 installation never contained the backup table.
    with sqlite3.connect(copy_dir / "runtime.sqlite") as connection:
        connection.execute("DROP TABLE file_operation_backups")
        connection.execute("DELETE FROM schema_migrations WHERE version=6")
    migrated = RuntimeStore(copy_dir / "runtime.sqlite")
    try:
        current = snapshot(migrated, session, run_id, workspace_id)
        assert current == expected
        with pytest.raises(ValueError, match="missing"):
            migrated.file_operation_backup(operation_id)
    finally:
        migrated.close()
    repeated = RuntimeStore(copy_dir / "runtime.sqlite")
    try:
        assert snapshot(repeated, session, run_id, workspace_id) == expected
    finally:
        repeated.close()
