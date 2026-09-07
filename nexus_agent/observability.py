"""SQLite-backed run, message, and event storage."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(api[_-]?key|authorization|token|secret|password)", re.I)


def redact(value: Any, *, max_text: int = 4_000) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item, max_text=max_text)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, max_text=max_text) for item in value]
    if isinstance(value, tuple):
        return [redact(item, max_text=max_text) for item in value]
    if isinstance(value, str) and len(value) > max_text:
        return value[:max_text] + f"… [{len(value) - max_text} chars truncated]"
    return value


class TraceStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._closed = False
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        """Flush and close SQLite resources (important on Windows)."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    role TEXT NOT NULL, content_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                    started_at REAL NOT NULL, completed_at REAL, output TEXT NOT NULL DEFAULT '',
                    steps INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL NOT NULL DEFAULT 0, usage_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
                    type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
            """)

    def create_session(self, session_id: str) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO sessions(id, created_at, updated_at) VALUES (?, ?, ?)",
                (session_id, now, now),
            )

    def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        self.create_session(session_id)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
            self._connection.execute(
                "INSERT INTO messages(session_id, seq, role, content_json) VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    row["next_seq"],
                    message["role"],
                    json.dumps(redact(message.get("content")), ensure_ascii=False),
                ),
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id)
            )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT role, content_json FROM messages WHERE session_id=? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [{"role": row["role"], "content": json.loads(row["content_json"])} for row in rows]

    def create_run(self, run_id: str, session_id: str) -> None:
        self.create_session(session_id)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO runs(id, session_id, status, started_at) VALUES (?, ?, 'running', ?)",
                (run_id, session_id, time.time()),
            )

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE run_id=?", (run_id,)
            ).fetchone()
            event = {
                "run_id": run_id,
                "seq": row["next_seq"],
                "ts": time.time(),
                "type": event_type,
                "payload": redact(payload),
            }
            self._connection.execute(
                "INSERT INTO events(run_id, seq, ts, type, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    event["seq"],
                    event["ts"],
                    event_type,
                    json.dumps(event["payload"], ensure_ascii=False),
                ),
            )
        return event

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        output: str,
        steps: int,
        tool_calls: int,
        duration_ms: float,
        usage: dict[str, int],
        error: str | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE runs SET status=?, completed_at=?, output=?, steps=?, tool_calls=?, "
                "duration_ms=?, usage_json=?, error=? WHERE id=?",
                (
                    status,
                    time.time(),
                    output,
                    steps,
                    tool_calls,
                    duration_ms,
                    json.dumps(usage),
                    error,
                    run_id,
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["usage"] = json.loads(result.pop("usage_json"))
        return result

    def get_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT seq, ts, type, payload_json FROM events "
                "WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after),
            ).fetchall()
        return [
            {
                "run_id": run_id,
                "seq": row["seq"],
                "ts": row["ts"],
                "type": row["type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def export_jsonl(self, run_id: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for event in self.get_events(run_id):
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return destination
