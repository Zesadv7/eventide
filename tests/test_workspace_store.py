"""Contracts for identity, ownership, and canonical facts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from eventide.store import RuntimeStore
from eventide.workspace import HostLease, canonical_workspace


def test_store_replay_order_and_terminal(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    workspace = store.register_workspace(isolated_workspace, None)
    assert store.register_workspace(isolated_workspace, None) == workspace
    store.create_session("s", workspace.workspace_id)
    store.create_run("r", "s")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: store.append_fact("s", "note", {"n": n}, run_id="r"), range(20)))
    assert [e["session_seq"] for e in store.session_events("s")] == list(range(1, 21))
    store.append_message("s", {"role": "user", "content": "hello"})
    store.finish_run("r", status="completed", output="done")
    store.finish_run("r", status="failed", output="late")
    assert store.get_run("r")["output"] == "done"
    with pytest.raises(ValueError, match="terminated"):
        store.append_fact("s", "model.response", {}, run_id="r")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._connection.execute("DELETE FROM runtime_events")
    store.close()
    reopened = RuntimeStore(isolated_workspace / "runtime.sqlite")
    assert reopened.load_messages("s") == [{"role": "user", "content": "hello"}]
    assert reopened.get_run("r")["status"] == "completed"
    reopened.close()


def test_host_lease_exclusive_and_releasable(isolated_workspace):
    lease = HostLease(isolated_workspace)
    with pytest.raises(RuntimeError, match="already owned"):
        HostLease(isolated_workspace)
    lease.close()
    HostLease(isolated_workspace).close()


def test_workspace_remove_and_binding(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    first = store.register_workspace(isolated_workspace, None)
    second = store.register_workspace(isolated_workspace / "second", None)
    store.create_session("s", first.workspace_id)
    with pytest.raises(ValueError, match="different workspace"):
        store.create_session("s", second.workspace_id)
    with pytest.raises(ValueError, match="orphan"):
        store.remove_workspace(first.workspace_id)
    store.remove_workspace(second.workspace_id)
    assert len(store.list_workspaces()) == 1
    assert canonical_workspace(isolated_workspace)[0].is_dir()
    store.close()


def test_session_metadata_archive_and_safe_delete(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    workspace = store.register_workspace(isolated_workspace, None)
    store.create_session("empty", workspace.workspace_id)
    renamed = store.update_session("empty", title="  Durable work\nrecord  ")
    assert renamed["title"] == "Durable work record"
    assert renamed["archived"] == 0
    store.update_session("empty", archived=True)
    assert store.list_sessions(workspace.workspace_id, include_archived=False) == []
    assert store.list_sessions(workspace.workspace_id, include_archived=True)[0]["archived"] == 1
    store.delete_session("empty")
    assert store.get_session("empty") is None

    store.create_session("history", workspace.workspace_id)
    store.create_run("run", "history")
    with pytest.raises(ValueError, match="archive"):
        store.delete_session("history")
    assert store._connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()[0] == 2
    store.close()


def test_run_history_pages_and_event_cursor_are_bounded(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    workspace = store.register_workspace(isolated_workspace, None)
    store.create_session("long", workspace.workspace_id)
    for index in range(5):
        run_id = f"run-{index}"
        store.create_run(run_id, "long")
        store.append_event(run_id, "message.user", {"message": {"content": f"work {index}"}})
        store.finish_run(run_id, status="completed", output=f"done {index}")

    latest = store.list_runs("long", limit=2)
    assert [run["id"] for run in latest] == ["run-3", "run-4"]
    older = store.list_runs("long", limit=2, before="run-3")
    assert [run["id"] for run in older] == ["run-1", "run-2"]
    cursor = store.run_events("run-4")[0]["session_seq"]
    assert all(event["session_seq"] > cursor for event in store.get_events("run-4", cursor))
    assert store.session_activity("long")["first_intent"] == "work 0"
    store.close()
