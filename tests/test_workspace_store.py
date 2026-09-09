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
