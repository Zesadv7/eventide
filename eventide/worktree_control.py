"""Durable user worktree operations with frozen catalog and Git ownership."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventide.models import RunResult
from eventide.workspace import digest
from eventide.worktrees import WorktreeRecord, WorktreeService

if TYPE_CHECKING:
    from eventide.host import RuntimeHost
    from eventide.store import RuntimeStore


def catalog_hash(service: WorktreeService, records: list[WorktreeRecord]) -> str:
    # Include the record file incarnation: deleting/recreating the same name and
    # commit must not validate an old destructive request.
    return digest([{
        "record": record.to_dict(),
        "incarnation": (stat.st_ino, stat.st_mtime_ns, stat.st_size),
    } for record in records if (stat := service._record_path(record.name).stat())])


def is_worktree_control_run(store: RuntimeStore, run_id: str) -> bool:
    run = store.get_run_identity(run_id)
    if run is None:
        return False
    intent: dict[str, Any] = next((e["payload"] for e in store.run_events(run_id)
                                  if e["type"] == "run.admitted"), {})
    session = store.get_session(run["session_id"])
    if session is None or intent.get("source") != "worktree_control":
        return False
    workspace = store.get_workspace(session["workspace_id"])
    return (
        intent.get("project_id") == workspace.project_id
        and intent.get("execution_binding") == {
            "kind": "worktree_control", "operation_id": run_id,
            "workspace_id": workspace.workspace_id,
        }
        and any(e["type"] == "control.session" and e["payload"] == {
            "kind": "worktree_control", "project_id": workspace.project_id,
        } for e in store.session_events(run["session_id"]))
    )


class WorktreeController:
    def __init__(self, host: RuntimeHost):
        self.host, self.store = host, host.store
        self._pending: set[str] = set()

    def owner(self, project_id: str, *, create: bool = True) -> str:
        project = self.store.get_project(project_id)
        owner = f"worktree_control_{project_id}"
        marker = {"kind": "worktree_control", "project_id": project_id}
        session = self.store.get_session(owner)
        if session is None and create:
            self.host.create_session(owner, workspace_id=project.source_workspace_id)
            self.store.append_fact(owner, "control.session", marker)
            self.store.update_session(owner, title="工作副本管理", archived=True)
        elif session is not None and (
            session["workspace_id"] != project.source_workspace_id
            or not any(e["type"] == "control.session" and e["payload"] == marker
                       for e in self.store.session_events(owner))
        ):
            raise ValueError("Worktree control Session identity conflict")
        return owner

    def service(self, project_id: str) -> WorktreeService:
        project = self.store.get_project(project_id)
        workspace = self.store.get_workspace(project.source_workspace_id)
        return WorktreeService(Path(workspace.path), self.host.state_dir, workspace.workspace_id)

    async def catalog(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        service = self.service(project_id)
        async with self.host._project_git(project_id):
            records = await asyncio.to_thread(service.list)
            version = catalog_hash(service, records)
        result = []
        for record in records:
            checkouts = [w for w in self.store.list_workspaces() if w.path == record.path]
            protected = any(
                self.store.list_sessions(workspace_id=w.workspace_id, include_archived=True)
                or self.store.workspace_has_execution_history(w.workspace_id)
                or self.host.workspace_busy(w.workspace_id)
                for w in checkouts
            )
            result.append({**record.to_dict(), "history_protected": bool(protected)})
        return {"project_id": project.project_id, "catalog_hash": version, "worktrees": result}

    async def admit(
        self, project_id: str, run_id: str, *, action: str, name: str,
        expected_catalog_hash: str, ref: str = "HEAD", request_fingerprint: str | None = None,
    ) -> str:
        from eventide.host import _Admission

        await self.host.initialize()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid operation identity")
        if action not in {"create", "keep", "remove"}:
            raise ValueError("Invalid worktree action")
        WorktreeService.validate_name(name)
        owner = self.owner(project_id)
        if self.host.session_busy(owner):
            raise ValueError("Project worktree operation is busy")
        if self.store.get_run_identity(run_id):
            raise ValueError("Run identity already used")
        service = self.service(project_id)
        async with self.host._project_git(project_id):
            records = await asyncio.to_thread(service.list)
            if catalog_hash(service, records) != expected_catalog_hash:
                raise ValueError("Worktree catalog changed; refresh before managing")
            if action == "create":
                if any(record.name == name for record in records):
                    raise ValueError("Managed worktree name already exists")
                frozen_ref = await asyncio.to_thread(service.resolve_ref, ref)
            else:
                if not any(record.name == name for record in records):
                    raise ValueError("Managed worktree not found")
                frozen_ref = ""
        # Recheck after awaits, before the synchronous durable admission.
        if self.host.session_busy(owner):
            raise ValueError("Project worktree operation is busy")
        workspace = self.host.sessions.workspace(owner)
        intent = {
            "source": "worktree_control", "project_id": project_id,
            "workspace_id": workspace.workspace_id,
            "execution_binding": {"kind": "worktree_control", "operation_id": run_id,
                                  "workspace_id": workspace.workspace_id},
            "request_fingerprint": request_fingerprint,
        }
        ticket = self.host.dispatch.scheduler.reserve_admission(run_id, owner, startable=True)
        try:
            self.store.admit_run(run_id, owner, [
                ("run.admitted", intent, "system", "runtime"),
                ("worktree.accepted", {
                    "action": action, "name": name, "ref": frozen_ref,
                    "expected_catalog_hash": expected_catalog_hash,
                }, "user", "runtime"),
            ])
            self._pending.add(run_id)
            self.host._admission_done[run_id] = _Admission(owner, asyncio.Event())
            self.host._admissions[self.host._execution_key(run_id, intent)] = run_id
            self.host.dispatch.admitted(ticket)
        except BaseException as exc:
            self._pending.discard(run_id)
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.scheduler.release_ticket(ticket)
            raise
        finally:
            self.host.dispatch.dispatch()
        return run_id

    def start(self, run_id: str) -> asyncio.Task[RunResult]:
        if (not is_worktree_control_run(self.store, run_id) or run_id not in self._pending
                or self.store.run_is_terminal(run_id) or self.host._closed):
            raise ValueError("Worktree operation is not awaiting execution")
        self._pending.remove(run_id)
        try:
            task = self.host.dispatch.start(run_id, lambda: self._execute(run_id))
        except BaseException as exc:
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.finished(run_id)
            raise
        self.host._track_run_task(run_id, task)
        return task

    async def _execute(self, run_id: str) -> RunResult:
        accepted = self.accepted(run_id)
        identity = self.store.get_run_identity(run_id)
        assert identity is not None
        owner = identity["session_id"]
        workspace = self.host.sessions.workspace(owner)
        started = time.perf_counter()
        try:
            async with self.host._session_execution(owner, run_id):
                await self.host.manage_worktree(
                    workspace.path, accepted["action"], name=accepted["name"],
                    ref=accepted["ref"], _run_id=run_id,
                    _catalog_hash=accepted["expected_catalog_hash"],
                )
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
        if not is_worktree_control_run(self.store, run_id):
            raise ValueError("Worktree operation not found")
        return next(e["payload"] for e in self.store.run_events(run_id)
                    if e["type"] == "worktree.accepted")

    def detail(self, run_id: str) -> dict[str, Any]:
        accepted = self.accepted(run_id)
        run = self.store.get_run_summary(run_id)
        assert run is not None
        workspace = self.host.sessions.workspace(run["session_id"])
        result = next((e["payload"] for e in reversed(self.store.run_events(run_id))
                       if e["type"] == "worktree.completed"), None)
        return {"run_id": run_id, "session_id": run["session_id"],
                "project_id": workspace.project_id, "status": run["status"],
                **accepted, "result": result, "error": run["error"],
                "events_url": f"/api/runs/{run_id}/events"}

    def history(self, project_id: str, *, limit: int = 20, before: str | None = None
                ) -> list[dict[str, Any]]:
        return [self.detail(run["id"]) for run in reversed(self.store.list_runs(
            self.owner(project_id, create=False), limit=limit, before=before,
        ))]
