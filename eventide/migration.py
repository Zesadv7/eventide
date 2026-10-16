"""Read-only import of the v0.2 TraceStore schema into the event-native store."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from eventide.normalization import redact
from eventide.store import RuntimeStore
from eventide.workspace import digest

MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_ROWS = 50_000
MAX_CELL_BYTES = 1024 * 1024
COLUMNS = {
    "sessions": ("id", "created_at", "updated_at"),
    "messages": ("session_id", "seq", "role", "content_json"),
    "runs": ("id", "session_id", "status", "started_at", "completed_at", "output",
             "steps", "tool_calls", "duration_ms", "usage_json", "error"),
    "events": ("run_id", "seq", "ts", "type", "payload_json"),
}
ORDER = {"sessions": "rowid", "messages": "session_id,seq", "runs": "started_at,rowid",
         "events": "run_id,seq"}

def decoded(value: str) -> Any:
    def invalid(value: str) -> None:
        raise ValueError("Legacy JSON contains nonfinite values")
    try:
        data = json.loads(value, parse_constant=invalid)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Invalid legacy JSON") from exc
    stack = [(data, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 32:
            raise ValueError("Legacy JSON nesting exceeds limit")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Legacy JSON contains nonfinite values")
    return data

def _read_bounded(source: Path) -> dict[str, list[dict[str, Any]]]:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("Legacy source must be a regular database file")
    size = source.stat().st_size
    wal = Path(str(source) + "-wal")
    if wal.exists():
        size += wal.stat().st_size
    if size > MAX_DATABASE_BYTES:
        raise ValueError("Legacy database exceeds 64 MiB limit")
    deadline = time.monotonic() + 10
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
        connection.row_factory = sqlite3.Row
        setlimit = getattr(connection, "setlimit", None)
        length_limit = getattr(sqlite3, "SQLITE_LIMIT_LENGTH", None)
        if setlimit is not None and length_limit is not None:
            setlimit(length_limit, MAX_CELL_BYTES)
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migrations" in tables:
            raise ValueError("Source is already an Eventide runtime database")
        result = {}
        rows, text_bytes = 0, 0
        for table, fields in COLUMNS.items():
            if table not in tables:
                raise ValueError("Missing legacy table: " + table)
            actual = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
            if not set(fields) <= actual:
                raise ValueError("Missing legacy columns: " + table)
            # Python 3.10 lacks sqlite3.setlimit. Reject oversized cells inside
            # the same read transaction before materializing rows on that runtime.
            oversized = " OR ".join(
                f"length(CAST({field} AS BLOB))>{MAX_CELL_BYTES}" for field in fields
            )
            if connection.execute(
                f"SELECT 1 FROM {table} WHERE {oversized} LIMIT 1"
            ).fetchone():
                raise ValueError("Legacy record exceeds cell size limit")
            values = []
            for row in connection.execute(
                f"SELECT {','.join(fields)} FROM {table} ORDER BY {ORDER[table]}"
            ):
                rows += 1
                if rows > MAX_ROWS or time.monotonic() > deadline:
                    raise ValueError("Legacy import row/time limit exceeded")
                value = dict(row)
                for cell in value.values():
                    if isinstance(cell, str):
                        text_bytes += len(cell.encode("utf8"))
                    elif isinstance(cell, bytes):
                        raise ValueError("Legacy text cannot contain binary blobs")
                if text_bytes > MAX_TEXT_BYTES:
                    raise ValueError("Legacy history exceeds 32 MiB text limit")
                for column, key in (("content_json", "content"), ("usage_json", "usage"),
                                    ("payload_json", "payload")):
                    if column in value:
                        value[key] = decoded(value.pop(column))
                values.append(value)
            result[table] = values
        return result

def validate_archive(data: dict[str, list[dict[str, Any]]]) -> None:
    import re
    identity = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    def ident(value: Any) -> str:
        if not isinstance(value, str) or not identity.fullmatch(value):
            raise ValueError("Invalid legacy identity")
        return value
    def number(value: Any, *, optional: bool = False, integer: bool = False) -> None:
        if optional and value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Invalid legacy numeric field")
        if not math.isfinite(value) or value < 0 or (integer and not isinstance(value, int)):
            raise ValueError("Invalid legacy numeric value")
    sessions: set[str] = set()
    runs: dict[str, str] = {}
    for row in data["sessions"]:
        key = ident(row["id"])
        if key in sessions:
            raise ValueError("Duplicate legacy Session identity")
        sessions.add(key)
        number(row["created_at"])
        number(row["updated_at"])
    for row in data["runs"]:
        key, owner = ident(row["id"]), ident(row["session_id"])
        if key in runs or owner not in sessions:
            raise ValueError("Duplicate or orphan legacy Run")
        runs[key] = owner
        number(row["started_at"])
        number(row["completed_at"], optional=True)
        for field in ("steps", "tool_calls"):
            number(row[field], integer=True)
        number(row["duration_ms"])
        if not isinstance(row["output"], str) or (
            row["error"] is not None and not isinstance(row["error"], str)
        ) or not isinstance(row["status"], str) or not isinstance(row["usage"], dict):
            raise ValueError("Invalid legacy Run fields")
    sequences: set[tuple[str, int]] = set()
    for row in data["messages"]:
        owner = ident(row["session_id"])
        number(row["seq"], integer=True)
        pair = owner, row["seq"]
        if owner not in sessions or pair in sequences:
            raise ValueError("Duplicate or orphan legacy message")
        sequences.add(pair)
        if row["role"] not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Invalid legacy message role")
        if not isinstance(row["content"], (str, list, dict)):
            raise ValueError("Invalid legacy message content")
    sequences.clear()
    for row in data["events"]:
        run = ident(row["run_id"])
        number(row["seq"], integer=True)
        number(row["ts"])
        pair = run, row["seq"]
        if run not in runs or pair in sequences:
            raise ValueError("Duplicate or orphan legacy event")
        sequences.add(pair)
        if not isinstance(row["type"], str) or len(row["type"]) > 128:
            raise ValueError("Invalid legacy event type")
        if not isinstance(row["payload"], dict):
            raise ValueError("Invalid legacy event payload")



def read_legacy_archive(source: Path) -> dict[str, Any]:
    """Read one consistent snapshot, validate it, and discard connection secrets."""
    try:
        data = _read_bounded(source)
        validate_archive(data)
        for table, fields in {
            "messages": ("content",), "runs": ("output", "error", "usage"),
            "events": ("payload",),
        }.items():
            for row in data[table]:
                for field in fields:
                    row[field] = redact(row[field], max_text=None)
        return data
    except (sqlite3.DatabaseError, OSError, UnicodeError, RecursionError) as exc:
        raise ValueError("Legacy database is unreadable or exceeds import limits") from exc


def import_v02_database(
    store: RuntimeStore,
    source: Path,
    workspace_id: str,
) -> dict[str, Any]:
    """Import one legacy database atomically without modifying the source file."""
    source = source.resolve()
    if source == store.path.resolve():
        raise ValueError("Legacy source and current runtime database must be different files")
    data = read_legacy_archive(source)
    return import_legacy_data(store, data, workspace_id, source_label=source.name)


def import_legacy_data(
    store: RuntimeStore, legacy: dict[str, Any], workspace_id: str, *,
    source_label: str, namespace: str = "",
    on_committed: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Commit validated, non-executable history and its owning receipt atomically."""
    validate_archive(legacy)
    workspace = store.get_workspace(workspace_id)
    if namespace:
        sessions = {row["id"]: "legacy_" + digest([namespace, "session", row["id"]])[:32]
                    for row in legacy["sessions"]}
        runs = {row["id"]: "legacy_" + digest([namespace, "run", row["id"]])[:32]
                for row in legacy["runs"]}
        legacy = {
            "sessions": [{**r, "original_id": r["id"], "id": sessions[r["id"]]}
                         for r in legacy["sessions"]],
            "messages": [{**r, "session_id": sessions[r["session_id"]]}
                         for r in legacy["messages"]],
            "runs": [{**r, "original_id": r["id"], "id": runs[r["id"]],
                      "session_id": sessions[r["session_id"]]} for r in legacy["runs"]],
            "events": [{**r, "run_id": runs[r["run_id"]]} for r in legacy["events"]],
        }
    session_ids = {row["id"] for row in legacy["sessions"]}
    if not namespace and any(
        key == "configuration_control" or key.startswith(
            ("index_control_", "worktree_control_", "evaluation_control_", "delivery_prepare_",
             "deliveries_", "worktrees_", "probe_"))
        for key in session_ids
    ):
        raise ValueError("Legacy Session uses a reserved runtime identity")
    if any(row["session_id"] not in session_ids for row in legacy["runs"]):
        raise ValueError("Legacy run references a missing session")
    run_ids = {row["id"] for row in legacy["runs"]}
    if any(row["run_id"] not in run_ids for row in legacy["events"]):
        raise ValueError("Legacy event references a missing run")

    with store._lock, store._connection:
        store._connection.execute("BEGIN IMMEDIATE")
        for table, keys in (("sessions", session_ids), ("runs", run_ids)):
            identifiers = sorted(keys)
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                existing = store._connection.execute(
                    f"SELECT id FROM {table} WHERE id IN ({placeholders})", batch,
                ).fetchone()
                if existing:
                    raise ValueError(f"Legacy identity already exists: {existing['id']}")

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
                "VALUES (?, ?, ?, ?, 1, '.')",
                (session_id, workspace_id, created_at,
                 "旧版导入 · " + session.get("original_id", session_id)[:48]),
            )
            insert_event(
                session_id, "control.session",
                {"kind": "legacy_history",
                 "original_session_id": redact(session.get("original_id", session_id),
                                               max_text=None)},
                created_at,
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
                {"source": source_label, "messages": len(messages_by_session[session_id]),
                 "read_only": True},
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
            insert_event(session_id, "run.started", {
                "source": "legacy_import", "mode": "history",
                "workspace_id": workspace_id, "project_id": workspace.project_id,
            }, started_at, run_id=run_id, turn_id=turn_id)
            insert_event(session_id, "legacy.run_summary", redact({
                "original_run_id": run.get("original_id", run_id),
                "usage": run["usage"], "status": run["status"],
            }, max_text=None), started_at, run_id=run_id, turn_id=turn_id)
            for event in events_by_run[run_id]:
                insert_event(
                    session_id,
                    "legacy.event",
                    redact({"original_type": event["type"], "payload": event["payload"]},
                           max_text=None),
                    float(event["ts"]),
                    run_id=run_id,
                    turn_id=turn_id,
                )
            status = run["status"] if run["status"] in {"completed", "failed"} else "interrupted"
            payload = {
                "output": redact(run["output"], max_text=None),
                "steps": run["steps"],
                "tool_calls": run["tool_calls"],
                "duration_ms": run["duration_ms"],
                "usage": {},
                "error": redact(run["error"], max_text=None),
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

        report = {
            "source": source_label, "workspace_id": workspace_id,
            "sessions": len(legacy["sessions"]), "runs": len(legacy["runs"]),
            "messages": len(legacy["messages"]), "events": len(legacy["events"]),
            "provider_config_imported": False, "api_key_imported": False,
            "read_only_history": True,
        }
        if on_committed is not None:
            on_committed(report)
    return report
