"""Read-only import of the v0.2 TraceStore schema into the event-native store."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from eventide.normalization import redact
from eventide.store import RuntimeStore

_REQUIRED_COLUMNS = {
    "sessions": {"id", "created_at", "updated_at"},
    "messages": {"session_id", "seq", "role", "content_json"},
    "runs": {
        "id",
        "session_id",
        "status",
        "started_at",
        "completed_at",
        "output",
        "steps",
        "tool_calls",
        "duration_ms",
        "usage_json",
        "error",
    },
    "events": {"run_id", "seq", "ts", "type", "payload_json"},
}
_EVENT_ALIASES = {"tool.request": "tool.prepared", "tool.result": "tool.completed"}
_TERMINALS = {"run.completed", "run.failed", "run.interrupted"}


def _decode_json(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in legacy {label}") from exc


def _read_legacy(source: Path) -> dict[str, Any]:
    if not source.is_file():
        raise ValueError(f"Legacy database not found: {source}")
    try:
        connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Cannot open legacy database: {source}") from exc
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "schema_migrations" in tables:
            raise ValueError("Source is already a versioned Eventide database")
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not required <= columns:
                missing = ", ".join(sorted(required - columns))
                raise ValueError(f"Not a supported v0.2 database: {table} missing {missing}")

        sessions = [dict(row) for row in connection.execute("SELECT * FROM sessions")]
        messages = [
            {**dict(row), "content": _decode_json(row["content_json"], "message")}
            for row in connection.execute(
                "SELECT * FROM messages ORDER BY session_id, seq"
            )
        ]
        runs = [
            {**dict(row), "usage": _decode_json(row["usage_json"], "run usage")}
            for row in connection.execute("SELECT * FROM runs ORDER BY started_at, rowid")
        ]
        events = [
            {**dict(row), "payload": _decode_json(row["payload_json"], "event")}
            for row in connection.execute("SELECT * FROM events ORDER BY run_id, seq")
        ]
        if any(not isinstance(run["usage"], dict) for run in runs):
            raise ValueError("Invalid JSON object in legacy run usage")
        if any(not isinstance(event["payload"], dict) for event in events):
            raise ValueError("Invalid JSON object in legacy event")
        provider = None
        if "provider_config" in tables:
            provider_row = connection.execute(
                "SELECT provider, base_url, model, updated_at FROM provider_config WHERE id=1"
            ).fetchone()
            provider = dict(provider_row) if provider_row else None
        return {
            "sessions": sessions,
            "messages": messages,
            "runs": runs,
            "events": events,
            "provider": provider,
        }
    except sqlite3.DatabaseError as exc:
        raise ValueError("Not a readable v0.2 TraceStore database") from exc
    finally:
        connection.close()


def import_v02_database(
    store: RuntimeStore,
    source: Path,
    workspace_id: str,
) -> dict[str, Any]:
    """Import one legacy database atomically without modifying the source file."""
    source = source.resolve()
    if source == store.path.resolve():
        raise ValueError("Legacy source and current runtime database must be different files")
    store.get_workspace(workspace_id)
    legacy = _read_legacy(source)
    session_ids = {row["id"] for row in legacy["sessions"]}
    if any(row["session_id"] not in session_ids for row in legacy["runs"]):
        raise ValueError("Legacy run references a missing session")
    run_ids = {row["id"] for row in legacy["runs"]}
    if any(row["run_id"] not in run_ids for row in legacy["events"]):
        raise ValueError("Legacy event references a missing run")

    with store._lock, store._connection:
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            existing = store._connection.execute(
                f"SELECT id FROM sessions WHERE id IN ({placeholders})", list(session_ids)
            ).fetchone()
            if existing:
                raise ValueError(
                    f"Session already exists in current state: {existing['id']}"
                )
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            existing = store._connection.execute(
                f"SELECT id FROM runs WHERE id IN ({placeholders})", list(run_ids)
            ).fetchone()
            if existing:
                raise ValueError(f"Run already exists in current state: {existing['id']}")

        messages_by_session: dict[str, list[dict[str, Any]]] = {
            session_id: [] for session_id in session_ids
        }
        for message in legacy["messages"]:
            if message["session_id"] not in messages_by_session:
                raise ValueError("Legacy message references a missing session")
            messages_by_session[message["session_id"]].append(message)
        events_by_run: dict[str, list[dict[str, Any]]] = {
            run_id: [] for run_id in run_ids
        }
        for event in legacy["events"]:
            events_by_run[event["run_id"]].append(event)

        next_seq = {session_id: 0 for session_id in session_ids}

        def insert_event(
            session_id: str,
            event_type: str,
            payload: dict[str, Any],
            timestamp: float,
            *,
            run_id: str | None = None,
            turn_id: str | None = None,
            role: str = "system",
        ) -> None:
            next_seq[session_id] += 1
            store._connection.execute(
                "INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)",
                (
                    uuid.uuid4().hex,
                    session_id,
                    next_seq[session_id],
                    turn_id,
                    run_id,
                    timestamp,
                    event_type,
                    role,
                    "v0.2-import",
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

        for session in legacy["sessions"]:
            session_id = session["id"]
            created_at = float(session["created_at"])
            store._connection.execute(
                "INSERT INTO sessions "
                "(id, workspace_id, created_at, title, archived, working_directory) "
                "VALUES (?, ?, ?, NULL, 0, '.')",
                (session_id, workspace_id, created_at),
            )
            for message in messages_by_session[session_id]:
                role = str(message["role"])
                insert_event(
                    session_id,
                    "message.imported",
                    {
                        "message": redact(
                            {"role": role, "content": message["content"]},
                            max_text=None,
                        )
                    },
                    created_at + int(message["seq"]) / 1_000_000,
                    role=role,
                )
            insert_event(
                session_id,
                "migration.v02",
                {"source": source.name, "messages": len(messages_by_session[session_id])},
                float(session["updated_at"]),
            )

        for run in legacy["runs"]:
            session_id = run["session_id"]
            run_id = run["id"]
            turn_id = f"turn_v02_{uuid.uuid4().hex[:16]}"
            started_at = float(run["started_at"])
            store._connection.execute(
                "INSERT INTO turns VALUES (?, ?, NULL, ?)",
                (turn_id, session_id, started_at),
            )
            store._connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?)",
                (run_id, session_id, turn_id, started_at),
            )
            for event in events_by_run[run_id]:
                if event["type"] in _TERMINALS:
                    continue
                insert_event(
                    session_id,
                    _EVENT_ALIASES.get(event["type"], event["type"]),
                    redact(event["payload"], max_text=None),
                    float(event["ts"]),
                    run_id=run_id,
                    turn_id=turn_id,
                )
            status = run["status"] if run["status"] in {"completed", "failed"} else "interrupted"
            payload = {
                "output": run["output"],
                "steps": run["steps"],
                "tool_calls": run["tool_calls"],
                "duration_ms": run["duration_ms"],
                "usage": run["usage"],
                "error": run["error"],
            }
            if status == "interrupted" and not payload["error"]:
                payload["error"] = "Imported v0.2 run had no terminal status"
            terminal_at = float(run["completed_at"] or started_at)
            insert_event(
                session_id,
                f"run.{status}",
                payload,
                terminal_at,
                run_id=run_id,
                turn_id=turn_id,
            )

        provider_imported = False
        if legacy["provider"] and store.get_provider_config() is None:
            provider = legacy["provider"]
            store._connection.execute(
                "INSERT INTO provider_config VALUES (1, ?, ?, ?, NULL, ?)",
                (
                    provider["provider"],
                    provider["base_url"],
                    provider["model"],
                    provider["updated_at"],
                ),
            )
            provider_imported = True

    return {
        "source": str(source),
        "workspace_id": workspace_id,
        "sessions": len(legacy["sessions"]),
        "runs": len(legacy["runs"]),
        "messages": len(legacy["messages"]),
        "events": len(legacy["events"]),
        "provider_config_imported": provider_imported,
        "api_key_imported": False,
    }
