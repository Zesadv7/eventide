"""Versioned SQLite identities and append-only canonical runtime facts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from eventide.models import RuntimeEvent, WorkspaceRecord
from eventide.normalization import redact
from eventide.projections import TERMINALS, MessagesProjection, RuntimeStateProjection


class RuntimeStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _initialize(self) -> None:
        with self._lock, self._connection:
            tables = {
                r[0]
                for r in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "sessions" in tables and "schema_migrations" not in tables:
                raise ValueError("Legacy database: choose a fresh v0.3 state root")
            version = 0
            if "schema_migrations" in tables:
                version = self._connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                if version not in {1, 2}:
                    raise ValueError(f"Unsupported runtime schema version: {version}")
            self._connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE, git_root TEXT, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                    continuation_of TEXT UNIQUE REFERENCES runs(id), created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                    turn_id TEXT NOT NULL REFERENCES turns, started_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_events (
                    event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                    session_seq INTEGER NOT NULL, turn_id TEXT REFERENCES turns,
                    run_id TEXT REFERENCES runs, ts REAL NOT NULL, type TEXT NOT NULL,
                    role TEXT NOT NULL, author TEXT NOT NULL, payload_json TEXT NOT NULL,
                    partial INTEGER NOT NULL DEFAULT 0, schema_version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(session_id, session_seq)
                );
                CREATE INDEX IF NOT EXISTS events_by_run ON runtime_events(run_id, session_seq);
                CREATE UNIQUE INDEX IF NOT EXISTS one_terminal ON runtime_events(run_id)
                    WHERE type IN ('run.completed', 'run.failed', 'run.interrupted');
                CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON runtime_events
                    BEGIN SELECT RAISE(ABORT, 'Runtime events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_event_delete BEFORE DELETE ON runtime_events
                    BEGIN SELECT RAISE(ABORT, 'Runtime events are immutable'); END;
                CREATE TABLE IF NOT EXISTS context_checkpoints (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                    covered_seq INTEGER NOT NULL, source_digest TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    policy_version INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_config (
                    id INTEGER PRIMARY KEY CHECK(id=1), provider TEXT NOT NULL, base_url TEXT,
                    model TEXT NOT NULL, api_key_ciphertext TEXT, updated_at REAL NOT NULL
                );
                INSERT OR IGNORE INTO schema_migrations VALUES (1, unixepoch());
            """)
            version = max(version, 1)
            if version < 2:
                columns = {
                    row[1]
                    for row in self._connection.execute("PRAGMA table_info(sessions)")
                }
                if "title" not in columns:
                    self._connection.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                if "archived" not in columns:
                    self._connection.execute(
                        "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
                    )
                self._connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (2, unixepoch())"
                )

    def register_workspace(self, path: Path, git_root: str | None) -> WorkspaceRecord:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO workspaces VALUES (?, ?, ?, ?, ?)",
                (f"ws_{uuid.uuid4().hex[:16]}", path.name, str(path), git_root, time.time()),
            )
            row = self._connection.execute(
                "SELECT * FROM workspaces WHERE path=?", (str(path),)
            ).fetchone()
        return WorkspaceRecord(**dict(row))

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Workspace not found: {workspace_id}")
        return WorkspaceRecord(**dict(row))

    def list_workspaces(self) -> list[WorkspaceRecord]:
        with self._lock:
            return [
                WorkspaceRecord(**dict(r))
                for r in self._connection.execute(
                    "SELECT * FROM workspaces ORDER BY created_at, workspace_id"
                )
            ]

    def remove_workspace(self, workspace_id: str) -> None:
        self.get_workspace(workspace_id)
        with self._lock, self._connection:
            if self.list_sessions(workspace_id):
                raise ValueError("Workspace has sessions; removal would orphan history")
            self._connection.execute("DELETE FROM workspaces WHERE workspace_id=?", (workspace_id,))

    def create_session(self, session_id: str, workspace_id: str | None = None) -> None:
        with self._lock, self._connection:
            existing = self.get_session(session_id)
            if existing:
                if workspace_id and existing["workspace_id"] != workspace_id:
                    raise ValueError("Session is bound to a different workspace")
                return
            if workspace_id is None:
                workspace_id = self.register_workspace(
                    self.path.parent.resolve(), None
                ).workspace_id
            self.get_workspace(workspace_id)
            self._connection.execute(
                "INSERT INTO sessions (id, workspace_id, created_at) VALUES (?, ?, ?)",
                (session_id, workspace_id, time.time()),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self, workspace_id: str, *, include_archived: bool = True
    ) -> list[dict[str, Any]]:
        where = "workspace_id=?" if include_archived else "workspace_id=? AND archived=0"
        with self._lock:
            return [
                dict(r)
                for r in self._connection.execute(
                    f"SELECT * FROM sessions WHERE {where} ORDER BY created_at",
                    (workspace_id,),
                )
            ]

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        if self.get_session(session_id) is None:
            raise ValueError(f"Session not found: {session_id}")
        updates: list[str] = []
        values: list[Any] = []
        if title is not None:
            cleaned = title.replace("\n", " ").strip()
            if not cleaned:
                raise ValueError("Session title cannot be empty")
            updates.append("title=?")
            values.append(cleaned[:96])
        if archived is not None:
            updates.append("archived=?")
            values.append(int(archived))
        if not updates:
            raise ValueError("No session changes supplied")
        values.append(session_id)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id=?",
                values,
            )
        record = self.get_session(session_id)
        assert record is not None
        return record

    def delete_session(self, session_id: str) -> None:
        if self.get_session(session_id) is None:
            raise ValueError(f"Session not found: {session_id}")
        with self._lock, self._connection:
            has_history = self._connection.execute(
                "SELECT EXISTS(SELECT 1 FROM runtime_events WHERE session_id=? "
                "UNION SELECT 1 FROM runs WHERE session_id=?)",
                (session_id, session_id),
            ).fetchone()[0]
            if has_history:
                raise ValueError("Session has history; archive it instead of deleting")
            self._connection.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def create_run(
        self, run_id: str, session_id: str, *, continuation_of: str | None = None
    ) -> str:
        self.create_session(session_id)
        turn_id = f"turn_{uuid.uuid4().hex[:16]}"
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?)",
                (turn_id, session_id, continuation_of, time.time()),
            )
            self._connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?)", (run_id, session_id, turn_id, time.time())
            )
        return turn_id

    def append_fact(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        role: str = "system",
        author: str = "runtime",
        partial: bool = False,
    ) -> dict[str, Any]:
        if partial and event_type in TERMINALS:
            raise ValueError("Terminal facts cannot be partial")
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            turn_id = None
            if run_id:
                run = self._connection.execute(
                    "SELECT * FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if not run or run["session_id"] != session_id:
                    raise ValueError("Run/session identity mismatch")
                turn_id = run["turn_id"]
                terminal = self._connection.execute(
                    "SELECT * FROM runtime_events WHERE run_id=? AND type IN "
                    "('run.completed', 'run.failed', 'run.interrupted')",
                    (run_id,),
                ).fetchone()
                if terminal:
                    if event_type in TERMINALS:
                        return self._decode(terminal)
                    raise ValueError("Run already terminated")
            seq = self._connection.execute(
                "SELECT COALESCE(MAX(session_seq),0)+1 FROM runtime_events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            event = RuntimeEvent(
                uuid.uuid4().hex,
                session_id,
                seq,
                turn_id,
                run_id,
                time.time(),
                event_type,
                role,
                author,
                payload,
                partial,
            )
            self._connection.execute(
                "INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    session_id,
                    seq,
                    turn_id,
                    run_id,
                    event.ts,
                    event_type,
                    role,
                    author,
                    json.dumps(payload, ensure_ascii=False),
                    int(partial),
                    1,
                ),
            )
        return asdict(event)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        event["payload"] = json.loads(event.pop("payload_json"))
        event["partial"] = bool(event["partial"])
        return event

    def session_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._decode(row)
                for row in self._connection.execute(
                    "SELECT * FROM runtime_events WHERE session_id=? ORDER BY session_seq",
                    (session_id,),
                )
            ]

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._decode(row)
                for row in self._connection.execute(
                    "SELECT * FROM runtime_events WHERE run_id=? ORDER BY session_seq", (run_id,)
                )
            ]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT r.*, t.continuation_of FROM runs r JOIN turns t ON r.turn_id=t.id "
                "WHERE r.id=?",
                (run_id,),
            ).fetchone()
        return (
            {**dict(row), **RuntimeStateProjection.project(self.run_events(run_id))}
            if row
            else None
        )

    def latest_run(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,)
            ).fetchone()
        return self.get_run(row[0]) if row else None

    def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        """Return all runs with projected state and lightweight event facts."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.*, t.continuation_of FROM runs r "
                "JOIN turns t ON r.turn_id=t.id "
                "WHERE r.session_id=? ORDER BY r.started_at, r.rowid",
                (session_id,),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            events = self.run_events(row["id"])
            record = {**dict(row), **RuntimeStateProjection.project(events)}
            record["event_count"] = len(events)
            record["checkpoint_count"] = sum(
                event["type"] == "workspace.checkpoint" for event in events
            )
            record["approval_count"] = sum(
                event["type"] == "approval.required" for event in events
            )
            record["last_seq"] = events[-1]["session_seq"] if events else 0
            records.append(record)
        return records

    def recover_interrupted(self) -> None:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM runs WHERE id NOT IN (SELECT run_id FROM runtime_events "
                "WHERE type IN ('run.completed','run.failed','run.interrupted'))"
            ).fetchall()
        for row in rows:
            self.append_event(
                row[0], "run.interrupted", {"error": "Host stopped before terminal fact"}
            )

    def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        self.create_session(session_id)
        self.append_fact(
            session_id,
            "message.imported",
            {"message": redact(message)},
            role=message["role"],
            author="compatibility",
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        return MessagesProjection.project(self.session_events(session_id))

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise ValueError("Run not found")
        event = self.append_fact(run["session_id"], event_type, redact(payload), run_id=run_id)
        return {**event, "seq": event["session_seq"]}

    def finish_run(self, run_id: str, *, status: str, **payload: Any) -> None:
        self.append_event(run_id, f"run.{status}", payload)

    def get_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        # Compatibility presentation; canonical names remain in run_events().
        aliases = {"tool.prepared": "tool.request", "tool.completed": "tool.result"}
        return [
            {**e, "seq": e["session_seq"], "type": aliases.get(e["type"], e["type"])}
            for e in self.run_events(run_id)
            if e["session_seq"] > after
        ]

    def export_jsonl(self, run_id: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for event in self.run_events(run_id):
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return destination

    def get_provider_config(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM provider_config WHERE id=1").fetchone()
        return dict(row) if row else None

    def save_provider_config(
        self, *, provider: str, base_url: str | None, model: str, api_key_ciphertext: str | None
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO provider_config VALUES (1, ?, ?, ?, ?, ?)",
                (provider, base_url, model, api_key_ciphertext, time.time()),
            )
        return self.get_provider_config() or {}

    def delete_provider_config(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM provider_config WHERE id=1")

    def checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self._connection.execute(
                    "SELECT * FROM context_checkpoints WHERE session_id=? ORDER BY id DESC",
                    (session_id,),
                )
            ]

    def save_checkpoint(
        self,
        session_id: str,
        covered_seq: int,
        source_digest: str,
        summary: str,
        provider: str,
        model: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO context_checkpoints "
                "(session_id, covered_seq, source_digest, summary, "
                "policy_version, provider, model) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (session_id, covered_seq, source_digest, summary, provider, model),
            )
