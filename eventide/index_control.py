"""Durable index-build control Runs use the existing admission and model budgets."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from eventide.budgets import BudgetExceeded
from eventide.config import Settings
from eventide.models import RunResult
from eventide.retrieval_runtime import (
    configuration,
    freeze_configuration,
    prepare_index,
    validate_frozen_configuration,
    validate_preparation,
)

if TYPE_CHECKING:
    from eventide.host import RuntimeHost
    from eventide.store import RuntimeStore


def is_index_run(store: RuntimeStore, run_id: str) -> bool:
    run = store.get_run_identity(run_id)
    if run is None:
        return False
    intent: dict[str, Any] = next((e["payload"] for e in store.run_events(run_id)
                   if e["type"] == "run.admitted"), {})
    session = store.get_session(run["session_id"])
    if session is None or intent.get("source") != "index":
        return False
    workspace = store.get_workspace(session["workspace_id"])
    return (intent.get("project_id") == workspace.project_id
            and intent.get("execution_binding") == {
                "kind": "index", "index_run_id": run_id,
                "workspace_id": workspace.workspace_id,
            } and any(e["type"] == "control.session" and e["payload"] == {
                "kind": "index", "project_id": workspace.project_id,
            } for e in store.session_events(run["session_id"])))


class IndexController:
    def __init__(self, host: RuntimeHost):
        self.host = host
        self.store = host.store
        self._settings: dict[str, Settings] = {}

    def owner(
        self, project_id: str, *, create: bool = True, workspace_id: str | None = None,
    ) -> str:
        project = self.store.get_project(project_id)
        target = workspace_id or project.source_workspace_id
        workspace = self.store.get_workspace(target)
        if workspace.project_id != project_id:
            raise ValueError("Index Workspace does not belong to this project")
        owner = (f"index_control_{project_id}" if target == project.source_workspace_id
                 else f"index_control_{project_id}_{target}")
        session = self.store.get_session(owner)
        marker = {"kind": "index", "project_id": project_id}
        if session is None and create:
            self.host.create_session(owner, workspace_id=target)
            self.store.append_fact(owner, "control.session", marker)
            self.store.update_session(owner, title="索引管理", archived=True)
        elif session is not None and (
            session["workspace_id"] != target
            or not any(e["type"] == "control.session" and e["payload"] == marker
                       for e in self.store.session_events(owner))
        ):
            raise ValueError("Index control Session identity conflict")
        return owner

    def configuration(self) -> dict[str, Any]:
        return configuration(self.host.settings)

    async def admit(
        self, project_id: str, run_id: str, *, config_hash: str, force: bool = False,
        code_structure: bool = False, source_scope: str = "code",
        request_fingerprint: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        from eventide.host import _Admission

        await self.host.initialize()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid index Run identity")
        config = self.configuration()
        frozen = freeze_configuration(self.host.settings, "prepare",
                                      code_structure=code_structure, source_scope=source_scope)
        if config_hash != config["config_hash"]:
            raise ValueError("Index configuration changed; refresh before starting")
        owner = self.owner(project_id, workspace_id=workspace_id)
        if self.host.session_busy(owner):
            raise ValueError("Project index build is busy")
        if self.store.get_run_identity(run_id):
            raise ValueError("Run identity already used")
        workspace = self.host.sessions.workspace(owner)
        validate_preparation(self.host, workspace)
        intent = {
            "source": "index", "project_id": project_id, "workspace_id": workspace.workspace_id,
            "execution_binding": {"kind": "index", "index_run_id": run_id,
                                  "workspace_id": workspace.workspace_id},
            "request_fingerprint": request_fingerprint,
        }
        ticket = self.host.dispatch.scheduler.reserve_admission(run_id, owner, startable=True)
        try:
            self.store.admit_run(run_id, owner, [
                ("run.admitted", intent, "system", "runtime"),
                ("index.accepted", {"configuration": config, "force": force,
                                    **frozen}, "system", "runtime"),
            ])
            self._settings[run_id] = self.host.settings
            self.host._admission_done[run_id] = _Admission(owner, asyncio.Event())
            self.host._admissions[self.host._execution_key(run_id, intent)] = run_id
            self.host.dispatch.admitted(ticket)
        except BaseException as exc:
            self._settings.pop(run_id, None)
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.scheduler.release_ticket(ticket)
            raise
        finally:
            self.host.dispatch.dispatch()
        return run_id

    def start(self, run_id: str) -> asyncio.Task[RunResult]:
        if (not is_index_run(self.store, run_id) or self.store.run_is_terminal(run_id)
                or run_id not in self._settings or self.host._closed
                or run_id in self.host._run_tasks):
            raise ValueError("Index build is not awaiting execution")
        settings = self._settings.pop(run_id)
        try:
            task = self.host.dispatch.start(run_id, lambda: self._execute(run_id, settings))
        except BaseException as exc:
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.finished(run_id)
            raise
        self.host._track_run_task(run_id, task)
        return task

    def _accepted(self, run_id: str) -> dict[str, Any]:
        if not is_index_run(self.store, run_id):
            raise ValueError("Index build not found")
        return next(e["payload"] for e in self.store.run_events(run_id)
                    if e["type"] == "index.accepted")

    async def _execute(self, run_id: str, settings: Settings) -> RunResult:
        accepted = self._accepted(run_id)
        identity = self.store.get_run_identity(run_id)
        assert identity is not None
        owner = identity["session_id"]
        workspace = self.host.sessions.workspace(owner)
        intent = self.host._admitted_intent(run_id)
        started = time.perf_counter()
        async with self.host._session_execution(owner, run_id):
            self.store.append_event(run_id, "run.started", {**(intent or {}), "mode": "index"})
            try:
                if self.store.workspace_has_unknown_outcome(workspace.workspace_id):
                    raise ValueError("Source Workspace has an unresolved tool outcome")
                validate_frozen_configuration(settings, accepted)
                stats = await prepare_index(
                    self.host, run_id, workspace, settings=settings, force=accepted["force"],
                    code_structure=accepted["code_structure"],
                    source_scope=accepted["source_scope"], frozen_configuration=accepted,
                )
                self.store.append_event(run_id, "index.published", {
                    "stats": asdict(stats), "generation": stats.generation,
                })
            except BaseException as exc:
                interrupted = isinstance(exc, (asyncio.CancelledError, BudgetExceeded))
                reason = ("cancelled" if isinstance(exc, asyncio.CancelledError) else
                          "token_budget" if isinstance(exc, BudgetExceeded) else "index_failed")
                self.store.append_event(
                    run_id, "run.interrupted" if interrupted else "run.failed",
                    {"reason": reason,
                     "error": reason if interrupted else self.host._clean(str(exc))},
                )
                raise
            duration = (time.perf_counter() - started) * 1000
            output = f"Index published: {stats.files} files, {stats.chunks} chunks"
            self.store.append_event(run_id, "run.completed", {
                "output": output, "steps": 0, "tool_calls": 0, "usage": {}, "duration_ms": duration,
            })
            return RunResult(run_id, owner, "completed", output, 0, 0, duration,
                             turn_id=identity["turn_id"])

    def detail(self, run_id: str) -> dict[str, Any]:
        accepted = self._accepted(run_id)
        run = self.store.get_run_summary(run_id)
        assert run is not None
        workspace = self.host.sessions.workspace(run["session_id"])
        published = next((e["payload"] for e in reversed(self.store.run_events(run_id))
                          if e["type"] == "index.published"), None)
        return {"run_id": run_id, "session_id": run["session_id"],
                "project_id": workspace.project_id, "workspace_id": workspace.workspace_id,
                "status": run["status"],
                "configuration": accepted["configuration"], "force": accepted["force"],
                "code_structure": accepted["code_structure"],
                "source_scope": accepted.get("source_scope"), "published": published,
                "error": run["error"], "events_url": f"/api/runs/{run_id}/events"}

    def list(self, project_id: str, *, limit: int = 20, before: str | None = None
             ) -> list[dict[str, Any]]:
        self.store.get_project(project_id)
        owners = [self.owner(project_id, workspace_id=w.workspace_id, create=False)
                  for w in self.store.list_workspaces() if w.project_id == project_id]
        runs = [run for owner in owners for run in self.store.list_runs(owner)]
        runs.sort(key=lambda run: (run["started_at"], run["id"]), reverse=True)
        if before is not None:
            position = next((i for i, run in enumerate(runs) if run["id"] == before), None)
            runs = [] if position is None else runs[position + 1:]
        return [self.detail(run["id"]) for run in runs[:limit]]
