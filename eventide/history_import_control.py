"""Preview immutable legacy history, then atomically import it after explicit review."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventide.migration import import_legacy_data, read_legacy_archive, validate_archive
from eventide.models import RunResult
from eventide.utils import owned_local_work

if TYPE_CHECKING:
    from eventide.host import RuntimeHost
    from eventide.store import RuntimeStore

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def is_history_import_run(store: RuntimeStore, run_id: str) -> bool:
    identity = store.get_run_identity(run_id)
    if identity is None:
        return False
    session = store.get_session(identity["session_id"])
    if session is None:
        return False
    workspace = store.get_workspace(session["workspace_id"])
    intent: dict[str, Any] = next((e["payload"] for e in store.run_events(run_id)
                                  if e["type"] == "run.admitted"), {})
    return (
        intent.get("source") == "history_import"
        and intent.get("project_id") == workspace.project_id
        and intent.get("execution_binding") == {
            "kind": "history_import", "operation_id": run_id,
            "workspace_id": workspace.workspace_id,
        }
        and any(e["type"] == "control.session" and e["payload"] == {
            "kind": "history_import", "project_id": workspace.project_id,
        } for e in store.session_events(identity["session_id"]))
    )


class HistoryImportController:
    def __init__(self, host: RuntimeHost):
        self.host, self.store = host, host.store
        self._pending: set[str] = set()
        self._sources: dict[str, Path] = {}

    def owner(self, project_id: str, *, create: bool = True) -> str:
        project = self.store.get_project(project_id)
        owner = f"history_import_{project_id}"
        marker = {"kind": "history_import", "project_id": project_id}
        session = self.store.get_session(owner)
        if session is None and create:
            self.host.create_session(owner, workspace_id=project.source_workspace_id)
            self.store.append_fact(owner, "control.session", marker)
            self.store.update_session(owner, title="旧版历史导入", archived=True)
        elif session is not None and (
            session["workspace_id"] != project.source_workspace_id
            or not any(e["type"] == "control.session" and e["payload"] == marker
                       for e in self.store.session_events(owner))
        ):
            raise ValueError("History import control identity conflict")
        return owner

    def _path(self, run_id: str, *, create: bool = False) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid import identity")
        directory = self.host.state_dir.resolve() / "history-imports"
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError("History archive directory changed")
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{run_id}.json"
        if path.is_symlink():
            raise ValueError("History archive path changed")
        return path

    def _snapshot(self, run_id: str, source: Path) -> dict[str, Any]:
        if source.resolve() == self.store.path.resolve():
            raise ValueError("Cannot import the current runtime database")
        data = read_legacy_archive(source)
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf8")
        if len(encoded) > MAX_ARCHIVE_BYTES:
            raise ValueError("History archive exceeds size limit")
        path = self._path(run_id, create=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return {"sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded), "counts": {key: len(data[key]) for key in
                                                ("sessions", "runs", "messages", "events")}}

    def _read_snapshot(self, preview_id: str, expected: str) -> dict[str, Any]:
        preview = self.detail(preview_id)
        if preview["action"] != "preview" or preview["status"] != "completed":
            raise ValueError("History preview is not completed")
        evidence = preview["preview"]
        if evidence is None or evidence["sha256"] != expected:
            raise ValueError("History preview hash changed")
        path = self._path(preview_id)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            encoded = handle.read(MAX_ARCHIVE_BYTES + 1)
        if len(encoded) != evidence["bytes"] or len(encoded) > MAX_ARCHIVE_BYTES:
            raise ValueError("History archive size changed")
        if hashlib.sha256(encoded).hexdigest() != expected:
            raise ValueError("History archive content changed")
        data: dict[str, Any] = json.loads(encoded)
        validate_archive(data)
        return data

    async def admit(
        self, project_id: str, run_id: str, *, action: str, source: str | None = None,
        preview_id: str | None = None, archive_sha256: str | None = None,
        request_fingerprint: str | None = None,
    ) -> str:
        from eventide.host import _Admission

        await self.host.initialize()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid import identity")
        if action not in {"preview", "import"}:
            raise ValueError("Invalid history import action")
        if action == "preview":
            if not source or not source.strip() or "\0" in source:
                raise ValueError("History source is required")
            accepted = {"action": action}
            source_path = Path(source).expanduser().absolute()
        else:
            if not preview_id or not archive_sha256:
                raise ValueError("Reviewed preview and hash are required")
            preview = self.detail(preview_id)
            if (preview["project_id"] != project_id or preview["action"] != "preview"
                    or preview["status"] != "completed" or preview["preview"] is None
                    or preview["preview"]["sha256"] != archive_sha256):
                raise ValueError("Preview does not match reviewed project and archive")
            accepted = {"action": action, "preview_id": preview_id,
                        "archive_sha256": archive_sha256}
            source_path = None
        owner = self.owner(project_id)
        if self.host.session_busy(owner):
            raise ValueError("Project history import is busy")
        if self.store.get_run_identity(run_id):
            raise ValueError("Run identity already used")
        workspace = self.host.sessions.workspace(owner)
        intent = {
            "source": "history_import", "project_id": project_id,
            "workspace_id": workspace.workspace_id,
            "execution_binding": {"kind": "history_import", "operation_id": run_id,
                                  "workspace_id": workspace.workspace_id},
            "request_fingerprint": request_fingerprint,
        }
        ticket = self.host.dispatch.scheduler.reserve_admission(run_id, owner, startable=True)
        try:
            self.store.admit_run(run_id, owner, [
                ("run.admitted", intent, "system", "runtime"),
                ("history.accepted", accepted, "user", "runtime"),
            ])
            if source_path is not None:
                self._sources[run_id] = source_path
            self._pending.add(run_id)
            self.host._admission_done[run_id] = _Admission(owner, asyncio.Event())
            self.host._admissions[self.host._execution_key(run_id, intent)] = run_id
            self.host.dispatch.admitted(ticket)
        except BaseException as exc:
            self._sources.pop(run_id, None)
            self._pending.discard(run_id)
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.scheduler.release_ticket(ticket)
            raise
        finally:
            self.host.dispatch.dispatch()
        return run_id

    def start(self, run_id: str) -> asyncio.Task[RunResult]:
        if (not is_history_import_run(self.store, run_id) or run_id not in self._pending
                or self.store.run_is_terminal(run_id) or self.host._closed):
            raise ValueError("History import is not awaiting execution")
        self._pending.remove(run_id)
        source = self._sources.pop(run_id, None)
        try:
            task = self.host.dispatch.start(run_id, lambda: self._execute(run_id, source))
        except BaseException as exc:
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.finished(run_id)
            raise
        self.host._track_run_task(run_id, task)
        return task

    def _import(self, run_id: str, accepted: dict[str, Any]) -> dict[str, Any]:
        data = self._read_snapshot(accepted["preview_id"], accepted["archive_sha256"])
        identity = self.store.get_run_identity(run_id)
        assert identity is not None
        owner = identity["session_id"]
        workspace = self.host.sessions.workspace(owner)

        def completed(report: dict[str, Any]) -> None:
            if any(e["type"] == "history.imported"
                   and e["payload"].get("preview_id") == accepted["preview_id"]
                   for e in self.store.session_events(owner)):
                raise ValueError("This history preview was already imported")
            self.store._append_fact_locked(owner, "history.imported", {
                "preview_id": accepted["preview_id"], "archive_sha256": accepted["archive_sha256"],
                "report": report,
            }, run_id=run_id, author="user")
            self.store._append_fact_locked(owner, "run.completed", {
                "output": f"Imported read-only history: {report['sessions']} sessions",
                "steps": 0, "tool_calls": 0, "usage": {},
            }, run_id=run_id)

        return import_legacy_data(
            self.store, data, workspace.workspace_id, namespace=accepted["preview_id"],
            source_label="reviewed legacy archive", on_committed=completed,
        )

    async def _execute(self, run_id: str, source: Path | None) -> RunResult:
        accepted = self.accepted(run_id)
        identity = self.store.get_run_identity(run_id)
        assert identity is not None
        owner = identity["session_id"]
        started = time.perf_counter()
        async with self.host._session_execution(owner, run_id):
            self.store.append_event(run_id, "run.started", {
                **(self.host._admitted_intent(run_id) or {}), "mode": "history_import",
            })
            try:
                if accepted["action"] == "preview":
                    assert source is not None
                    evidence = await owned_local_work(self._snapshot, run_id, source)
                    self.store.append_event(run_id, "history.previewed", evidence)
                    self.store.append_event(run_id, "run.completed", {
                        "output": "History preview ready for review", "steps": 0,
                        "tool_calls": 0, "usage": {},
                    })
                else:
                    await owned_local_work(self._import, run_id, accepted)
            except BaseException as exc:
                if not self.store.run_is_terminal(run_id):
                    self.store.append_event(
                        run_id, "run.interrupted" if isinstance(exc, asyncio.CancelledError)
                        else "run.failed",
                        {"error": "cancelled" if isinstance(exc, asyncio.CancelledError)
                         else self.host._clean(str(exc))},
                    )
                raise
        summary = self.store.get_run_summary(run_id)
        assert summary is not None
        return RunResult(run_id, owner, "completed", summary["output"], 0, 0,
                         (time.perf_counter() - started) * 1000, turn_id=identity["turn_id"])

    def accepted(self, run_id: str) -> dict[str, Any]:
        if not is_history_import_run(self.store, run_id):
            raise ValueError("History import operation not found")
        return next(e["payload"] for e in self.store.run_events(run_id)
                    if e["type"] == "history.accepted")

    def detail(self, run_id: str) -> dict[str, Any]:
        accepted = self.accepted(run_id)
        summary = self.store.get_run_summary(run_id)
        assert summary is not None
        workspace = self.host.sessions.workspace(summary["session_id"])
        events = self.store.run_events(run_id)
        preview = next((e["payload"] for e in events if e["type"] == "history.previewed"), None)
        result = next((e["payload"]["report"] for e in events
                       if e["type"] == "history.imported"), None)
        return {"run_id": run_id, "session_id": summary["session_id"],
                "project_id": workspace.project_id, **accepted, "status": summary["status"],
                "preview": preview, "result": result, "error": summary["error"],
                "imported": any(e["type"] == "history.imported"
                                and e["payload"].get("preview_id") == run_id
                                for e in self.store.session_events(summary["session_id"])),
                "events_url": f"/api/runs/{run_id}/events"}

    def history(self, project_id: str, *, limit: int = 20, before: str | None = None
                ) -> list[dict[str, Any]]:
        return [self.detail(run["id"]) for run in reversed(self.store.list_runs(
            self.owner(project_id, create=False), limit=limit, before=before,
        ))]
