"""Versioned SQLite identities and append-only canonical runtime facts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar

from eventide import interactions
from eventide.models import ProjectRecord, RuntimeEvent, WorkspaceRecord
from eventide.normalization import redact
from eventide.projections import TERMINALS, MessagesProjection, RuntimeStateProjection
from eventide.workspace import digest, project_source

_FactResult = TypeVar("_FactResult")


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
                if version not in {1, 2, 3, 4, 5, 6}:
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
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    source_workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces,
                    name TEXT NOT NULL, git_common_dir TEXT UNIQUE, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_projects (
                    workspace_id TEXT PRIMARY KEY REFERENCES workspaces,
                    project_id TEXT NOT NULL REFERENCES projects,
                    kind TEXT NOT NULL CHECK(kind IN ('source','worktree'))
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
                    row[1] for row in self._connection.execute("PRAGMA table_info(sessions)")
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
                version = 2
            if version < 3:
                columns = {
                    row[1] for row in self._connection.execute("PRAGMA table_info(sessions)")
                }
                if "working_directory" not in columns:
                    self._connection.execute(
                        "ALTER TABLE sessions ADD COLUMN working_directory "
                        "TEXT NOT NULL DEFAULT '.'"
                    )
                self._connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (3, unixepoch())"
                )
            if version < 4:
                # Only add identity relationships; preserve all historic IDs and facts.
                rows = self._connection.execute(
                    "SELECT * FROM workspaces ORDER BY created_at, workspace_id"
                ).fetchall()
                for row in rows:
                    source, common = project_source(Path(row["path"]), row["git_root"])
                    self._bind_project(dict(row), source, common)
                self._connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (4, unixepoch())"
                )
            if version < 5:
                # Memory remains canonical events; only add a read-acceleration index.
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS memory_fact_types ON runtime_events(type) "
                    "WHERE partial=0 AND type IN "
                    "('memory.record','memory.suggestion','memory.settings','memory.observed')"
                )
                self._connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (5, unixepoch())"
                )
            if version < 6:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS file_operation_backups ("
                    "operation_id TEXT PRIMARY KEY, encrypted_content TEXT NOT NULL)"
                )
                self._connection.execute(
                    "INSERT INTO schema_migrations VALUES (6, unixepoch())"
                )

    def prepare_file_operation(self, record: dict[str, Any], encrypted_content: str) -> None:
        """Commit the complete recovery artifact and its preparation fact together."""
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "INSERT INTO file_operation_backups VALUES (?,?)",
                (record["operation_id"], encrypted_content),
            )
            self._append_fact_locked(record["session_id"], "file.operation", record)

    def file_operation_backup(self, operation_id: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT encrypted_content FROM file_operation_backups WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise ValueError("File operation backup is missing")
        return str(row[0])

    def _bind_project(
        self, row: dict[str, Any], source: Path, common: str | None,
    ) -> None:
        """Create immutable identities within the caller's SQLite transaction."""
        if self._connection.execute(
            "SELECT 1 FROM workspace_projects WHERE workspace_id=?", (row["workspace_id"],)
        ).fetchone():
            return
        project = self._connection.execute(
            "SELECT * FROM projects WHERE git_common_dir=?", (common,)
        ).fetchone() if common else None
        if project is None:
            source_row = self._connection.execute(
                "SELECT * FROM workspaces WHERE path=?", (str(source),)
            ).fetchone()
            if source_row is None:
                source_row = {
                    "workspace_id": f"ws_{uuid.uuid4().hex[:16]}", "name": source.name,
                    "path": str(source), "git_root": str(source) if common else None,
                    "created_at": time.time(),
                }
                self._connection.execute(
                    "INSERT INTO workspaces VALUES "
                    "(:workspace_id,:name,:path,:git_root,:created_at)",
                    source_row,
                )
            project = self._connection.execute(
                "SELECT p.* FROM projects p JOIN workspace_projects w USING(project_id) "
                "WHERE w.workspace_id=?", (source_row["workspace_id"],)
            ).fetchone()
            if project is None:
                project = {
                    "project_id": f"project_{uuid.uuid4().hex[:16]}",
                    "source_workspace_id": source_row["workspace_id"],
                    "name": source_row["name"], "git_common_dir": common,
                    "created_at": source_row["created_at"],
                }
                self._connection.execute(
                    "INSERT INTO projects VALUES "
                    "(:project_id,:source_workspace_id,:name,:git_common_dir,:created_at)",
                    project,
                )
                self._connection.execute(
                    "INSERT INTO workspace_projects VALUES (?,?,'source')",
                    (source_row["workspace_id"], project["project_id"]),
                )
            elif project["git_common_dir"] is None and common is not None:
                # A registered plain directory may later become a Git repository.
                # Enrich its identity evidence without changing any association.
                self._connection.execute(
                    "UPDATE projects SET git_common_dir=? WHERE project_id=?",
                    (common, project["project_id"]),
                )
            elif project["git_common_dir"] != common:
                raise ValueError("Project source identity changed; existing binding is immutable")
        kind = "source" if row["workspace_id"] == project["source_workspace_id"] else "worktree"
        self._connection.execute(
            "INSERT OR IGNORE INTO workspace_projects VALUES (?,?,?)",
            (row["workspace_id"], project["project_id"], kind),
        )

    def register_workspace(
        self, path: Path, git_root: str | None, *,
        on_registered: Callable[[WorkspaceRecord], None] | None = None,
    ) -> WorkspaceRecord:
        path = path.expanduser().resolve()
        with self._lock, self._connection:
            if on_registered:
                self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT workspace_id FROM workspaces WHERE path=?", (str(path),)
            ).fetchone()
            if existing is not None:
                record = self.get_workspace(existing["workspace_id"])
                if on_registered:
                    on_registered(record)
                return record
            source, common = project_source(path, git_root)
            self._connection.execute(
                "INSERT OR IGNORE INTO workspaces VALUES (?, ?, ?, ?, ?)",
                (f"ws_{uuid.uuid4().hex[:16]}", path.name, str(path), git_root, time.time()),
            )
            row = self._connection.execute(
                "SELECT * FROM workspaces WHERE path=?", (str(path),)
            ).fetchone()
            self._bind_project(dict(row), source, common)
            record = self.get_workspace(row["workspace_id"])
            if on_registered:
                on_registered(record)
            return record

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT w.*,a.project_id,a.kind,p.source_workspace_id FROM workspaces w "
                "JOIN workspace_projects a USING(workspace_id) "
                "JOIN projects p USING(project_id) "
                "WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Workspace not found: {workspace_id}")
        return WorkspaceRecord(**dict(row))

    def list_workspaces(self) -> list[WorkspaceRecord]:
        with self._lock:
            return [
                WorkspaceRecord(**dict(r))
                for r in self._connection.execute(
                    "SELECT w.*,a.project_id,a.kind,p.source_workspace_id FROM workspaces w "
                    "JOIN workspace_projects a USING(workspace_id) "
                    "JOIN projects p USING(project_id) "
                    "ORDER BY w.created_at, workspace_id"
                )
            ]

    def workspace_has_execution_history(self, workspace_id: str) -> bool:
        with self._lock:
            return bool(self._connection.execute(
                "SELECT EXISTS(SELECT 1 FROM runtime_events WHERE partial=0 "
                "AND type='run.admitted' "
                "AND json_extract(payload_json,'$.execution_binding.execution_workspace_id')=?)",
                (workspace_id,),
            ).fetchone()[0])

    def remove_workspace(
        self, workspace_id: str, *, on_removed: Callable[[], None] | None = None,
    ) -> None:
        with self._lock, self._connection:
            if on_removed:
                self._connection.execute("BEGIN IMMEDIATE")
            workspace = self.get_workspace(workspace_id)
            if (self.list_sessions(workspace_id)
                    or self.workspace_has_execution_history(workspace_id)):
                raise ValueError("Workspace has sessions; removal would orphan history")
            if workspace.kind == "source" and len(
                self.list_project_workspaces(workspace.project_id)
            ) > 1:
                raise ValueError("Project source has execution copies; removal would orphan them")
            self._connection.execute(
                "DELETE FROM workspace_projects WHERE workspace_id=?", (workspace_id,)
            )
            if workspace.kind == "source":
                self._connection.execute(
                    "DELETE FROM projects WHERE project_id=?", (workspace.project_id,)
                )
            self._connection.execute("DELETE FROM workspaces WHERE workspace_id=?", (workspace_id,))
            if on_removed:
                on_removed()

    def get_project(self, project_id: str) -> ProjectRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Project not found: {project_id}")
        return ProjectRecord(**dict(row))

    def list_projects(self) -> list[ProjectRecord]:
        with self._lock:
            return [ProjectRecord(**dict(row)) for row in self._connection.execute(
                "SELECT * FROM projects ORDER BY created_at, project_id"
            )]

    def list_project_workspaces(self, project_id: str) -> list[WorkspaceRecord]:
        self.get_project(project_id)
        return [item for item in self.list_workspaces() if item.project_id == project_id]

    def list_project_sessions(
        self, project_id: str, *, include_archived: bool = True,
    ) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self._lock:
            return [dict(row) for row in self._connection.execute(
                "SELECT s.* FROM sessions s JOIN workspace_projects w USING(workspace_id) "
                "WHERE w.project_id=? " + ("" if include_archived else "AND s.archived=0 ")
                + "ORDER BY s.created_at,s.id", (project_id,),
            )]

    def create_session(
        self,
        session_id: str,
        workspace_id: str | None = None,
        *,
        working_directory: str | None = None,
        mutation: tuple[str, str] | None = None,
    ) -> None:
        with self._lock, self._connection:
            if mutation is not None:
                self._connection.execute("BEGIN IMMEDIATE")
            existing = self.get_session(session_id)
            if existing:
                if mutation is not None:
                    raise ValueError("Session creation identity already exists")
                if workspace_id and existing["workspace_id"] != workspace_id:
                    raise ValueError("Session is bound to a different workspace")
                if (
                    working_directory is not None
                    and existing["working_directory"] != working_directory
                ):
                    raise ValueError("Session is bound to a different working directory")
                return
            if workspace_id is None:
                if mutation is not None:
                    raise ValueError("Atomic Session creation requires a Workspace")
                workspace_id = self.register_workspace(
                    self.path.parent.resolve(), None
                ).workspace_id
            self.get_workspace(workspace_id)
            self._connection.execute(
                "INSERT INTO sessions "
                "(id, workspace_id, created_at, working_directory) VALUES (?, ?, ?, ?)",
                (session_id, workspace_id, time.time(), working_directory or "."),
            )
            if mutation is not None:
                owner, identity = mutation
                self._append_fact_locked(
                    owner,
                    "session.created",
                    {
                        "target_session_id": session_id,
                        "workspace_id": workspace_id,
                        "working_directory": working_directory or ".",
                    },
                    author="user",
                )
                self._append_fact_locked(
                    owner,
                    "http.mutation_completed",
                    {
                        "identity": identity,
                        "result": {"session_id": session_id},
                    },
                    author="user",
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

    def session_is_control(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._connection.execute(
                "SELECT EXISTS(SELECT 1 FROM runtime_events WHERE session_id=? AND "
                "(type='control.session' OR (type='run.started' AND "
                "json_extract(payload_json,'$.source') IN ('delivery','verification'))))",
                (session_id,),
            ).fetchone()[0])

    def session_is_imported(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._connection.execute(
                "SELECT EXISTS(SELECT 1 FROM runtime_events WHERE session_id=? "
                "AND type='control.session' "
                "AND json_extract(payload_json,'$.kind')='legacy_history')", (session_id,),
            ).fetchone()[0])

    def imported_messages(
        self, session_id: str, *, limit: int = 50, before: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.session_is_imported(session_id):
            raise ValueError("Imported history Session not found")
        if not 1 <= limit <= 100 or (before is not None and before < 1):
            raise ValueError("Invalid imported message page")
        with self._lock:
            records = self._connection.execute(
                "SELECT session_seq,payload_json FROM runtime_events "
                "WHERE session_id=? AND type='message.imported' "
                "AND (? IS NULL OR session_seq<?) ORDER BY session_seq DESC LIMIT ?",
                (session_id, before, before, limit),
            ).fetchall()
        return [{"seq": row["session_seq"], **json.loads(row["payload_json"])["message"]}
                for row in reversed(records)]

    def session_metadata_hash(self, session_id: str) -> str:
        record = self.get_session(session_id)
        if record is None:
            raise ValueError("Session not found")
        return digest({key: record[key] for key in (
            "id", "workspace_id", "created_at", "working_directory", "title", "archived",
        )})

    def commit_session_metadata(
        self, owner: str, session_id: str, expected_hash: str, *,
        mutation_id: str, delete: bool = False, title: str | None = None,
        archived: bool | None = None, result: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """A metadata write and its successful transport receipt commit together."""
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if self.session_metadata_hash(session_id) != expected_hash:
                raise ValueError("Session metadata changed; refresh before editing")
            record = self.get_session(session_id)
            assert record is not None
            if delete:
                exists = self._connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM runtime_events WHERE session_id=? "
                    "UNION SELECT 1 FROM runs WHERE session_id=?)", (session_id, session_id),
                ).fetchone()[0]
                if exists:
                    raise ValueError("Session has history; archive it instead of deleting")
                self._connection.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                kind, metadata = "session.deleted", {}
            else:
                if title is None and archived is None:
                    raise ValueError("No session changes supplied")
                cleaned = title.replace("\n", " ").strip() if title is not None else record["title"]
                if title is not None and not cleaned:
                    raise ValueError("Session title cannot be empty")
                self._connection.execute(
                    "UPDATE sessions SET title=?, archived=? WHERE id=?",
                    (cleaned, int(archived) if archived is not None else record["archived"],
                     session_id),
                )
                kind = "session.metadata_changed"
                metadata = {"title": cleaned,
                            "archived": (archived if archived is not None
                                         else bool(record["archived"]))}
            self._append_fact_locked(owner, kind, {
                "target_session_id": session_id, "created_at": record["created_at"],
                "workspace_id": record["workspace_id"], "metadata": metadata,
            }, author="user")
            response = result()
            self._append_fact_locked(owner, "http.mutation_completed", {
                "identity": mutation_id, "result": response,
            }, author="user")
            return response

    def create_run(
        self, run_id: str, session_id: str, *, continuation_of: str | None = None
    ) -> str:
        self.create_session(session_id)
        with self._lock, self._connection:
            return self._create_run_locked(run_id, session_id, continuation_of)

    def _create_run_locked(self, run_id: str, session_id: str, continuation_of: str | None) -> str:
        turn_id = f"turn_{uuid.uuid4().hex[:16]}"
        self._connection.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?)",
            (turn_id, session_id, continuation_of, time.time()),
        )
        self._connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?)", (run_id, session_id, turn_id, time.time())
        )
        return turn_id

    def admit_run(
        self,
        run_id: str,
        session_id: str,
        facts: list[tuple[str, dict[str, Any], str, str]],
        *,
        continuation_of: str | None = None,
    ) -> str:
        """Commit admission identity and all its facts together, or write nothing."""
        if self.get_session(session_id) is None:
            raise ValueError(f"Session not found: {session_id}")
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            turn_id = self._create_run_locked(run_id, session_id, continuation_of)
            for event_type, payload, role, author in facts:
                self._append_fact_locked(
                    session_id, event_type, payload, run_id=run_id, role=role, author=author
                )
        return turn_id

    def claim_run_recovery(
        self,
        session_id: str,
        old_run_id: str,
        new_run_id: str,
        *,
        finalize_payload: dict[str, Any],
        claim_payload: dict[str, Any],
    ) -> bool:
        """Finalize a crashed Run and freeze its successor identity in one transaction.

        The claim is the only writer of ``recovery.claimed``: repeated Host
        initialization or consecutive restarts cannot mint a second successor,
        because the old Run is terminal and the claimed identity is durable
        before any successor admission is attempted.
        """
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            old = self._connection.execute(
                "SELECT session_id FROM runs WHERE id=?", (old_run_id,)
            ).fetchone()
            if old is None or old["session_id"] != session_id:
                return False
            if self._connection.execute(
                "SELECT 1 FROM runtime_events WHERE run_id=? AND type IN "
                "('run.completed','run.failed','run.interrupted')",
                (old_run_id,),
            ).fetchone():
                return False
            if self._connection.execute(
                "SELECT 1 FROM runtime_events WHERE run_id=? AND type='recovery.claimed'",
                (old_run_id,),
            ).fetchone():
                return False
            if self._connection.execute(
                "SELECT 1 FROM turns WHERE continuation_of=?", (old_run_id,)
            ).fetchone():
                return False
            if self._connection.execute(
                "SELECT 1 FROM runs WHERE id=?", (new_run_id,)
            ).fetchone():
                return False
            # The claim freezes the successor identity first: after the terminal
            # fact lands in the same transaction no further Run fact is accepted.
            self._append_fact_locked(
                session_id, "recovery.claimed", claim_payload,
                run_id=old_run_id, role="system", author="runtime",
            )
            self._append_fact_locked(
                session_id, "run.interrupted", finalize_payload,
                run_id=old_run_id, role="system", author="runtime",
            )
        return True

    def referenced_attachments(self, session_id: str) -> set[str]:
        """Canonical references protect attachments even when the used cache was not written."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM runtime_events WHERE session_id=? AND partial=0 "
                "AND type IN ('run.admitted', 'message.user')",
                (session_id,),
            ).fetchall()
        references: set[str] = set()
        for row in rows:
            payload = json.loads(row[0])
            references.update(str(item) for item in payload.get("attachment_ids", []))
            content = payload.get("message", {}).get("content", [])
            if isinstance(content, list):
                references.update(
                    str(block["attachment_id"])
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "attachment"
                )
        return references

    def delivery_facts(self, delivery_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._decode(row)
                for row in self._connection.execute(
                    "SELECT * FROM runtime_events WHERE partial=0 AND type LIKE 'delivery.%' "
                    "AND json_extract(payload_json, '$.delivery_id')=? ORDER BY rowid",
                    (delivery_id,),
                )
            ]

    def facts_by_type(self, types: tuple[str, ...]) -> list[dict[str, Any]]:
        """Read all committed facts of selected types, without navigation pagination."""
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        with self._lock:
            return [
                self._decode(row)
                for row in self._connection.execute(
                    "SELECT * FROM runtime_events WHERE partial=0 AND type IN ("
                    + placeholders + ") ORDER BY rowid",
                    types,
                )
            ]

    def transact_facts(
        self,
        session_id: str,
        types: tuple[str, ...],
        decide: Callable[
            [list[dict[str, Any]]],
            tuple[_FactResult, list[tuple[str, dict[str, Any]]]],
        ],
        *,
        author: str = "runtime",
        run_id: str | None = None,
    ) -> _FactResult:
        """Read, decide and append in one SQLite transaction across writers.

        The trusted callback is synchronous and must not mutate Store itself.
        Session facts may reference terminated Runs without changing their history.
        """
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if self.get_session(session_id) is None:
                raise ValueError(f"Session not found: {session_id}")
            result, additions = decide(self.facts_by_type(types))
            for event_type, payload in additions:
                self._append_fact_locked(
                    session_id, event_type, payload, author=author, run_id=run_id,
                )
            return result

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
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            return self._append_fact_locked(
                session_id,
                event_type,
                payload,
                run_id=run_id,
                role=role,
                author=author,
                partial=partial,
            )

    def workspace_has_unknown_outcome(self, workspace_id: str) -> bool:
        with self._lock:
            return bool(
                self._connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM runtime_events e "
                    "JOIN sessions s ON s.id=e.session_id "
                    "WHERE e.type='tool.outcome_unknown' AND e.partial=0 "
                    "AND COALESCE(json_extract(e.payload_json, '$.workspace_id'),s.workspace_id)=? "
                    "AND COALESCE(json_extract(e.payload_json, '$.readonly'),0)=0)",
                    (workspace_id,),
                ).fetchone()[0]
            )

    def generation_config(
        self, scope: str, identity: str, model_identity: str | None = None,
    ) -> dict[str, Any]:
        """Latest immutable configuration event; no independent mutable fact table."""
        with self._lock:
            where = " AND json_extract(payload_json,'$.model_identity')=?" if model_identity else ""
            arguments = (scope, identity, model_identity) if model_identity else (scope, identity)
            row = self._connection.execute(
                "SELECT payload_json FROM runtime_events WHERE partial=0 "
                "AND type='generation.configured' "
                "AND json_extract(payload_json,'$.scope')=? "
                "AND json_extract(payload_json,'$.identity')=?" + where
                + " ORDER BY rowid DESC LIMIT 1",
                arguments,
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def workspace_execution_config(self, workspace_id: str) -> dict[str, Any]:
        """A delivery's explicit execution choice survives later runs and restarts."""
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM runtime_events e JOIN sessions s ON s.id=e.session_id "
                "WHERE s.workspace_id=? AND e.type IN ('run.admitted','run.started') "
                "AND COALESCE(json_extract(e.payload_json,'$.source'),'') != 'delivery' "
                "AND json_extract(e.payload_json,'$.execution_config.writer_delivery_id') "
                "IS NOT NULL "
                "ORDER BY e.rowid DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
        return json.loads(row[0])["execution_config"] if row else {}

    def _append_fact_locked(
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
        turn_id = None
        if run_id:
            run = self._connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
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

    def session_activity(self, session_id: str) -> dict[str, Any]:
        """Return list-view facts without decoding the full session log."""
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        with self._lock:
            updated_at = self._connection.execute(
                "SELECT MAX(ts) FROM runtime_events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            intent = self._connection.execute(
                "SELECT payload_json FROM runtime_events "
                "WHERE session_id=? AND type IN ('message.user', 'message.imported') "
                "ORDER BY session_seq LIMIT 1",
                (session_id,),
            ).fetchone()
        first_intent = None
        if intent:
            first_intent = json.loads(intent[0]).get("message", {}).get("content")
        return {
            "first_intent": first_intent,
            "updated_at": updated_at or session["created_at"],
        }

    def run_events(
        self, run_id: str, *, after: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM runtime_events WHERE run_id=? AND session_seq>? ORDER BY session_seq"
        parameters: list[Any] = [run_id, after]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            return [self._decode(row) for row in self._connection.execute(query, parameters)]

    def worker_admissions(self, parent_run_id: str) -> list[dict[str, Any]]:
        """Read only child admission facts linked to this exact parent Run."""
        with self._lock:
            return [
                self._decode(row)
                for row in self._connection.execute(
                    "SELECT * FROM runtime_events WHERE partial=0 AND type='run.admitted' "
                    "AND json_extract(payload_json,'$.parent_run_id')=? ORDER BY rowid",
                    (parent_run_id,),
                )
            ]

    def usage_events(
        self, *, session_id: str | None = None, run_id: str | None = None,
        include_children: bool = True,
    ) -> list[dict[str, Any]]:
        """Select exclusive call facts and optional descendants across all history pages."""
        if (session_id is None) == (run_id is None):
            raise ValueError("Choose exactly one Session or Run usage scope")
        if session_id and self.get_session(session_id) is None:
            raise ValueError("Session not found")
        if run_id and self.get_run_identity(run_id) is None:
            raise ValueError("Run not found")
        selector = "session_id" if session_id else "id"
        descendants = (
            " UNION SELECT e.run_id FROM runtime_events e JOIN selected p "
            "ON json_extract(e.payload_json,'$.parent_run_id')=p.id "
            "WHERE e.type IN ('run.admitted','run.started') AND e.partial=0 "
            "AND e.run_id IS NOT NULL"
        ) if include_children else ""
        with self._lock:
            rows = self._connection.execute(
                f"WITH RECURSIVE selected(id) AS (SELECT id FROM runs WHERE {selector}=?"
                + descendants + ") SELECT e.*,s.workspace_id FROM runtime_events e "
                "JOIN selected p ON e.run_id=p.id JOIN sessions s ON e.session_id=s.id "
                "WHERE e.partial=0 AND e.type IN ('model.attempt_started',"
                "'model.attempt_finished','model.response','context.summary_generated') "
                "ORDER BY e.rowid",
                (session_id or run_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def acceptance_output_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runtime_events WHERE partial=0 AND type IN "
                "('acceptance.output_started','acceptance.output_recorded',"
                "'acceptance.output_expired','acceptance.output_cleanup_failed') ORDER BY rowid"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get_acceptance_output(
        self, session_id: str, run_id: str, call_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM runtime_events WHERE session_id=? "
                "AND json_extract(payload_json, '$.source_run_id')=? "
                "AND type='acceptance.output_recorded' AND partial=0 "
                "AND json_extract(payload_json, '$.call_id')=? ORDER BY session_seq DESC LIMIT 1",
                (session_id, run_id, call_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def run_terminal_at(self, run_id: str) -> float | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(ts) FROM runtime_events WHERE run_id=? "
                "AND type IN ('run.completed','run.failed','run.interrupted')",
                (run_id,),
            ).fetchone()
        return row[0]

    def get_tool_result(self, session_id: str, run_id: str, call_id: str) -> dict[str, Any] | None:
        """Return one completed result only when both identities belong to the session."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM runtime_events "
                "WHERE session_id=? AND run_id=? AND type='tool.completed' "
                "ORDER BY session_seq",
                (session_id, run_id),
            ).fetchall()
        for row in rows:
            payload = json.loads(row[0])
            if payload.get("call_id") == call_id:
                content = payload.get("content", "")
                return {**payload, "content": content if isinstance(content, str) else str(content)}
        return None

    def task_plan_state(self, session_id: str) -> dict[str, Any] | None:
        """Project the latest plan payload, including its allocated id sequence.

        Legacy payloads written before task identity existed carry no `next_task_seq`;
        callers fall back to the highest id they can see. Nothing is ever written back.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM runtime_events "
                "WHERE session_id=? AND type='task.plan_updated' AND partial=0 "
                "ORDER BY session_seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        todos = payload.get("todos", [])
        return {
            "todos": [dict(todo) for todo in todos] if isinstance(todos, list) else [],
            "next_task_seq": payload.get("next_task_seq"),
            "step": payload.get("step"),
        }

    def task_plan(self, session_id: str) -> list[dict[str, Any]] | None:
        """Project the latest task plan directly from its canonical event."""
        state = self.task_plan_state(session_id)
        return state["todos"] if state else None

    def task_events(self, session_id: str) -> list[dict[str, Any]]:
        """Read only the facts a task projection needs, not the whole session log."""
        with self._lock:
            return [
                self._decode(row)
                for row in self._connection.execute(
                    "SELECT * FROM runtime_events WHERE session_id=? AND type IN "
                    "('task.plan_updated', 'tool.completed', 'tool.abandoned') "
                    "ORDER BY session_seq",
                    (session_id,),
                )
            ]

    def get_run_identity(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT r.*, t.continuation_of FROM runs r JOIN turns t ON r.turn_id=t.id "
                "WHERE r.id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def run_is_terminal(self, run_id: str) -> bool:
        with self._lock:
            return bool(
                self._connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM runtime_events WHERE run_id=? "
                    "AND type IN ('run.completed','run.failed','run.interrupted'))",
                    (run_id,),
                ).fetchone()[0]
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.get_run_identity(run_id)
        return {**row, **RuntimeStateProjection.project(self.run_events(run_id))} if row else None

    def run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        """Read one projection and its event cursor under the same store lock."""
        with self._lock:
            identity = self.get_run_identity(run_id)
            if identity is None:
                return None
            events = self.run_events(run_id)
            return {
                "high_water": events[-1]["session_seq"] if events else 0,
                "state": {**identity, **RuntimeStateProjection.project(events)},
            }

    def get_run_summary(self, run_id: str) -> dict[str, Any] | None:
        """Project only state needed by navigation and history indexes."""
        row = self.get_run_identity(run_id)
        if row is None:
            return None
        with self._lock:
            events = [
                self._decode(event)
                for event in self._connection.execute(
                    "SELECT * FROM runtime_events WHERE run_id=? AND type IN "
                    "('run.admitted','run.started','approval.required','approval.resolved','run.completed',"
                    "'run.failed','run.interrupted') ORDER BY session_seq",
                    (run_id,),
                )
            ]
            activity = self._connection.execute(
                "SELECT MAX(ts) last_activity_at, "
                "COALESCE(MAX(CASE WHEN type='model.response' THEN "
                "CAST(json_extract(payload_json, '$.step') AS INTEGER) END), 0) steps, "
                "COALESCE(SUM(CASE WHEN type='tool.prepared' THEN 1 ELSE 0 END), 0) "
                "tool_calls "
                "FROM runtime_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
        state = RuntimeStateProjection.project(events)
        return {
            **row,
            **{
                key: state[key]
                for key in (
                    "status",
                    "output",
                    "duration_ms",
                    "error",
                    "pending_approvals",
                    "reason",
                    "usage",
                )
            },
            "steps": activity["steps"],
            "tool_calls": activity["tool_calls"],
            "last_activity_at": activity["last_activity_at"] or row["started_at"],
            **{
                key: state[key]
                for key in ("parent_run_id", "worker_role", "snapshot_id")
                if key in state
            },
            **({"completed_at": state["completed_at"]} if "completed_at" in state else {}),
        }

    def latest_run(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,)
            ).fetchone()
        return self.get_run(row[0]) if row else None

    def latest_run_summary(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self.get_run_summary(row[0]) if row else None

    def list_runs(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a newest-first page, presented in chronological order."""
        if limit is not None and limit < 1:
            raise ValueError("Run page limit must be positive")
        conditions = ["r.session_id=?"]
        parameters: list[Any] = [session_id]
        if before is not None:
            conditions.append(
                "r.rowid < COALESCE((SELECT rowid FROM runs WHERE id=? AND session_id=?), -1)"
            )
            parameters.extend([before, session_id])
        query = (
            "SELECT r.*, t.continuation_of FROM runs r "
            "JOIN turns t ON r.turn_id=t.id "
            f"WHERE {' AND '.join(conditions)} ORDER BY r.rowid DESC"
        )
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = list(reversed(self._connection.execute(query, parameters).fetchall()))
            events_by_run: dict[str, list[dict[str, Any]]] = {row["id"]: [] for row in rows}
            aggregates: dict[str, sqlite3.Row] = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                for event in self._connection.execute(
                    f"SELECT * FROM runtime_events WHERE run_id IN ({placeholders}) "
                    "AND type IN ('approval.required','approval.resolved','run.completed',"
                    "'run.failed','run.interrupted') "
                    "ORDER BY session_seq",
                    [row["id"] for row in rows],
                ):
                    events_by_run[event["run_id"]].append(self._decode(event))
                aggregates = {
                    aggregate["run_id"]: aggregate
                    for aggregate in self._connection.execute(
                        f"SELECT run_id, COUNT(*) event_count, MAX(session_seq) last_seq, "
                        "SUM(type='workspace.checkpoint') checkpoint_count, "
                        "SUM(type='approval.required') approval_count "
                        f"FROM runtime_events WHERE run_id IN ({placeholders}) GROUP BY run_id",
                        [row["id"] for row in rows],
                    )
                }
        records: list[dict[str, Any]] = []
        for row in rows:
            events = events_by_run[row["id"]]
            state = RuntimeStateProjection.project(events)
            record = {
                **dict(row),
                **{
                    key: state[key]
                    for key in (
                        "status",
                        "output",
                        "duration_ms",
                        "error",
                        "pending_approvals",
                        "reason",
                        "usage",
                        "gap_files",
                    )
                },
            }
            if "completed_at" in state:
                record["completed_at"] = state["completed_at"]
            aggregate = aggregates.get(row["id"])
            record["event_count"] = aggregate["event_count"] if aggregate else 0
            record["checkpoint_count"] = aggregate["checkpoint_count"] if aggregate else 0
            record["approval_count"] = aggregate["approval_count"] if aggregate else 0
            record["last_seq"] = aggregate["last_seq"] if aggregate else 0
            records.append(record)
        return records

    def unfinished_runs(self) -> list[str]:
        """Runs that never reached a terminal fact, oldest first: the Host died mid-run."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM runs WHERE id NOT IN (SELECT run_id FROM runtime_events "
                "WHERE type IN ('run.completed','run.failed','run.interrupted')) "
                "ORDER BY started_at"
            ).fetchall()
        return [row["id"] for row in rows]

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
        run = self.get_run_identity(run_id)
        if not run:
            raise ValueError("Run not found")
        event = self.append_fact(run["session_id"], event_type, redact(payload), run_id=run_id)
        return {**event, "seq": event["session_seq"]}

    def finish_run(self, run_id: str, *, status: str, **payload: Any) -> None:
        self.append_event(run_id, f"run.{status}", payload)

    def record_supplement(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append a supplement receipt; terminal runs only accept session facts."""
        run = self.get_run_identity(run_id)
        if not run:
            raise ValueError("Run not found")
        active = self.get_run(run_id)
        assert active is not None
        if active["status"] in interactions.ACTIVE_RUN_STATUSES:
            return self.append_fact(
                run["session_id"], interactions.SUPPLEMENT_EVENT, redact(payload),
                run_id=run_id, role="user", author="user",
            )
        return self.append_fact(
            run["session_id"], interactions.SUPPLEMENT_EVENT, redact(payload),
            role="user", author="user",
        )

    def interaction_supplements(self, run_id: str) -> list[dict[str, Any]]:
        """Narrow interaction projection scoped to one run's own session facts."""
        run = self.get_run_identity(run_id)
        if not run:
            raise ValueError("Run not found")
        receipts: dict[str, dict[str, Any]] = {}
        consumed: dict[str, dict[str, Any]] = {}
        for event in self.session_events(run["session_id"]):
            payload = event["payload"]
            if payload.get("run_id") != run_id:
                continue
            if event["type"] == interactions.SUPPLEMENT_EVENT:
                receipts[payload["supplement_id"]] = payload
            elif event["type"] == interactions.SUPPLEMENT_CONSUMED_EVENT:
                consumed[payload["supplement_id"]] = payload
        projected = interactions.merge_supplements(list(receipts.values()), consumed)
        if self.run_is_terminal(run_id):
            for item in projected:
                if item["status"] == "pending":
                    item["status"] = "late"
        return projected

    def consume_supplement(
        self, run_id: str, supplement_id: str, *, step: int, message: dict[str, Any]
    ) -> bool:
        """Atomically append the user message and its one-time consumption fact."""
        run = self.get_run_identity(run_id)
        if not run:
            raise ValueError("Run not found")
        session_id = run["session_id"]
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            receipt = self._connection.execute(
                "SELECT COUNT(*) FROM runtime_events WHERE session_id=? AND type=? "
                "AND json_extract(payload_json,'$.supplement_id')=? "
                "AND json_extract(payload_json,'$.run_id')=?",
                (session_id, interactions.SUPPLEMENT_EVENT, supplement_id, run_id),
            ).fetchone()[0]
            consumed = self._connection.execute(
                "SELECT COUNT(*) FROM runtime_events WHERE session_id=? AND type=? "
                "AND json_extract(payload_json,'$.supplement_id')=? "
                "AND json_extract(payload_json,'$.run_id')=?",
                (session_id, interactions.SUPPLEMENT_CONSUMED_EVENT, supplement_id, run_id),
            ).fetchone()[0]
            if not receipt or consumed:
                return False
            self._append_fact_locked(
                session_id, "message.user", {"message": redact(message)},
                run_id=run_id, role="user", author="user",
            )
            self._append_fact_locked(
                session_id, interactions.SUPPLEMENT_CONSUMED_EVENT,
                {"supplement_id": supplement_id, "run_id": run_id, "step": step},
                run_id=run_id,
            )
        return True

    def get_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        # Compatibility presentation; canonical names remain in run_events().
        aliases = {"tool.prepared": "tool.request", "tool.completed": "tool.result"}
        return [
            {**e, "seq": e["session_seq"], "type": aliases.get(e["type"], e["type"])}
            for e in self.run_events(run_id, after=after)
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

    def commit_configuration(
        self, owner: str, facts: list[tuple[str, dict[str, Any]]],
        result: Callable[[], dict[str, Any]], *, mutation_id: str | None = None,
        provider_update: tuple[dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Commit a configuration and its HTTP receipt together, never plaintext keys.

        The trusted result callback reads the new state in this transaction. A
        missing provider_update leaves it alone; (None,) deletes the saved row.
        """
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if provider_update is not None:
                record = provider_update[0]
                if record is None:
                    self._connection.execute("DELETE FROM provider_config WHERE id=1")
                else:
                    self._connection.execute(
                        "INSERT OR REPLACE INTO provider_config VALUES (1, ?, ?, ?, ?, ?)",
                        (record["provider"], record["base_url"], record["model"],
                         record["api_key_ciphertext"], time.time()),
                    )
            for kind, payload in facts:
                self._append_fact_locked(owner, kind, payload, author="user")
            response = result()
            if mutation_id:
                self._append_fact_locked(owner, "http.mutation_completed", {
                    "identity": mutation_id, "result": response,
                }, author="user")
            return response

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
