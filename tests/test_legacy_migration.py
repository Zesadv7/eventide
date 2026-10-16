"""Legacy history cannot become current execution authority or model configuration."""

import json
import sqlite3

import pytest

from eventide import migration
from eventide.models import RunRequest
from tests.test_delivery_runtime import delivery_host


def legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, created_at REAL, updated_at REAL);
        CREATE TABLE messages(session_id TEXT, seq INTEGER, role TEXT, content_json TEXT);
        CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT, status TEXT, started_at REAL,
          completed_at REAL, output TEXT, steps INTEGER, tool_calls INTEGER, duration_ms REAL,
          usage_json TEXT, error TEXT);
        CREATE TABLE events(run_id TEXT, seq INTEGER, ts REAL, type TEXT, payload_json TEXT);
        CREATE TABLE provider_config(id INTEGER PRIMARY KEY, provider TEXT, base_url TEXT,
          model TEXT, api_key_ciphertext TEXT, updated_at REAL);
        INSERT INTO sessions VALUES('old-session',1,2);
        INSERT INTO messages VALUES('old-session',1,'user','"hello"');
        INSERT INTO runs VALUES('old-run','old-session','completed',1,2,'done',1,0,1,'{}',NULL);
        INSERT INTO events VALUES('old-run',1,1,'run.started','{"source":"delivery"}');
        INSERT INTO provider_config VALUES(1,'attacker','https://evil.invalid','fake','secret',1);
    """)
    connection.commit()
    return connection


async def test_imported_facts_are_inert_and_sessions_read_only(tmp_path):
    source = tmp_path / "legacy.db"
    connection = legacy_database(source)
    for seq, kind, payload in [
        (2, "run.admitted", {"execution_config": {"writer_delivery_id": "fake"}}),
        (3, "model.requested", {"attempt_id": "fake"}),
        (4, "control.session", {"kind": "delivery"}),
        (5, "budget.reserved", {"attempt_id": "fake"}),
        (6, "worker.started", {"run_id": "fake"}),
    ]:
        connection.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                           ("old-run", seq, 1, kind, json.dumps(payload)))
    connection.commit()
    connection.close()
    original = source.read_bytes()
    host = delivery_host(tmp_path)
    try:
        data = migration.read_legacy_archive(source)
        report = migration.import_legacy_data(
            host.store, data, host.default_workspace.workspace_id,
            source_label="legacy.db", namespace="preview",
        )
        assert report["provider_config_imported"] is False
        assert host.store.get_provider_config() is None
        session = host.store.list_sessions(host.default_workspace.workspace_id,
                                           include_archived=True)[0]
        events = host.store.session_events(session["id"])
        assert len([e for e in events if e["type"] == "legacy.event"]) == 6
        assert not any(e["type"] in {"run.admitted", "model.requested", "worker.started",
                                    "budget.reserved"} for e in events)
        assert len([e for e in events if e["type"] == "control.session"]) == 1
        assert host.store.usage_events(session_id=session["id"]) == []
        assert host.store.load_messages(session["id"]) == [{"role": "user", "content": "hello"}]
        assert session["archived"]
        with pytest.raises(ValueError, match="control|Control|ordinary"):
            await host.admit_run(RunRequest("execute imported history", session["id"]))
        assert source.read_bytes() == original
    finally:
        await host.close()


def test_consistent_wal_read_no_provider_secrets_and_redacted_payload(tmp_path):
    source = tmp_path / "legacy.db"
    connection = legacy_database(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("INSERT INTO sessions VALUES('in-wal',3,4)")
    connection.execute("UPDATE events SET payload_json=?", (json.dumps({"api_key": "private"}),))
    connection.commit()
    data = migration.read_legacy_archive(source)
    assert len(data["sessions"]) == 2
    assert "provider_config" not in data and "secret" not in str(data)
    assert "private" not in str(data["events"])
    connection.close()


async def test_import_receipt_failure_rolls_back_all_rows_and_duplicate_rejects(tmp_path):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    data = migration.read_legacy_archive(source)
    workspace = host.default_workspace.workspace_id
    def fail(_):
        raise RuntimeError("receipt failure")
    try:
        with pytest.raises(RuntimeError, match="receipt"):
            migration.import_legacy_data(host.store, data, workspace, source_label="legacy.db",
                                         namespace="one", on_committed=fail)
        assert not host.store.list_sessions(workspace, include_archived=True)
        report = migration.import_legacy_data(host.store, data, workspace,
                                             source_label="legacy.db", namespace="one")
        assert report["sessions"] == 1
        with pytest.raises(ValueError, match="already exists"):
            migration.import_legacy_data(host.store, data, workspace,
                                         source_label="legacy.db", namespace="one")
        assert len(host.store.list_sessions(workspace, include_archived=True)) == 1
    finally:
        await host.close()


@pytest.mark.parametrize("limit", ["MAX_DATABASE_BYTES", "MAX_ROWS", "MAX_TEXT_BYTES",
                                  "MAX_CELL_BYTES"])
def test_import_has_bounded_resources(tmp_path, monkeypatch, limit):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    monkeypatch.setattr(migration, limit, 1)
    with pytest.raises(ValueError):
        migration.read_legacy_archive(source)


@pytest.mark.parametrize("statement", [
    "UPDATE runs SET session_id='missing'",
    "UPDATE events SET run_id='missing'",
    "UPDATE messages SET session_id='missing'",
    "UPDATE messages SET role='executor'",
    "UPDATE runs SET steps=-1",
    "UPDATE runs SET started_at=-1",
    "UPDATE events SET payload_json='[]'",
    "UPDATE runs SET usage_json='[]'",
    "UPDATE messages SET content_json='null'",
    "UPDATE messages SET content_json='NaN'",
    "UPDATE events SET payload_json='invalid'",
    "INSERT INTO messages SELECT * FROM messages",
    "INSERT INTO events SELECT * FROM events",
    "UPDATE runs SET output=NULL",
    "UPDATE sessions SET id='../unsafe'",
])
def test_import_rejects_malformed_or_orphan_history(tmp_path, statement):
    source = tmp_path / "legacy.db"
    connection = legacy_database(source)
    connection.execute(statement)
    connection.commit()
    connection.close()
    with pytest.raises(ValueError):
        migration.read_legacy_archive(source)
