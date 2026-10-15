"""Host-owned dispatch of durably admitted roots; no independent runtime state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from eventide.models import RunResult
from eventide.scheduler import AdmissionTicket, QueueEntry, RootLease, RuntimeScheduler

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


class RunDispatch:
    """Keep temporary tasks and resource ownership beside the canonical scheduler.

    Only RuntimeHost calls this helper. Recovery has already parked old unfinished
    Runs before it is constructed. It never owns SQLite state or replays work.
    """

    def __init__(self, host: RuntimeHost):
        self.host = host
        self.scheduler = RuntimeScheduler(host.store)
        self._leases: dict[str, RootLease] = {}
        self._ready: dict[str, asyncio.Future[None]] = {}

    def admitted(self, ticket: AdmissionTicket) -> None:
        lease = self.scheduler.commit_admission(ticket)
        if lease:
            self._leases[ticket.run_id] = lease
        else:
            # Queued work keeps its Session acceptance owner, never an execution
            # claim that would block unrelated source/copy operations.
            self.host._release_execution_claims(ticket.run_id)

    def start(
        self, run_id: str, execute: Callable[[], Awaitable[RunResult]],
    ) -> asyncio.Task[RunResult]:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ready[run_id] = ready

        async def run() -> RunResult:
            await ready
            return await execute()

        operation = run()
        try:
            task = asyncio.create_task(operation)
        except BaseException:
            operation.close()
            raise
        if run_id in self._leases:
            ready.set_result(None)
        else:
            self.dispatch()
        return task

    def _claim(self, entry: QueueEntry) -> bool:
        if entry.run_id not in self._ready or self._ready[entry.run_id].done():
            return False
        key = self.host._execution_key(entry.run_id, entry.intent)
        if (key in self.host._admissions or self.host._workspace_locks[key].locked()
                or self.host._session_locks[entry.session_id].locked()):
            return False
        self.host._admissions[key] = entry.run_id
        return True

    def dispatch(self) -> None:
        if self.host._closed:
            return
        while lease := self.scheduler.claim_next(self._claim):
            self._leases[lease.run_id] = lease
            self._ready[lease.run_id].set_result(None)

    def finished(self, run_id: str) -> None:
        """Called after terminal persistence and actual task/resource cleanup."""
        ready = self._ready.pop(run_id, None)
        if ready is not None and not ready.done():
            ready.cancel()
        lease = self._leases.pop(run_id, None)
        if lease is not None:
            self.scheduler.release_root(lease)
        self.dispatch()

    def snapshot(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id is not None:
            self.host.store.get_project(project_id)
        queued: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        position = 0
        for entry in self.scheduler.entries():
            owned = entry.run_id in self._leases
            if not owned and entry.status not in {"queued", "running"}:
                continue
            running = owned or entry.status == "running"
            if not running:
                position += 1
            if project_id is not None and entry.project_id != project_id:
                continue
            record = self.host.store.get_run_summary(entry.run_id)
            assert record is not None
            state = ("finishing" if entry.status not in {"queued", "running"} else
                     "starting" if owned and entry.status == "queued" else
                     "running" if running else "queued")
            item = {
                "run_id": entry.run_id, "session_id": entry.session_id,
                "project_id": entry.project_id, "workspace_id": entry.intent["workspace_id"],
                "execution": self.host.run_execution(entry.run_id),
                "admitted_at": entry.admitted_at, "position": None if running else position,
                "status": record["status"], "dispatch_state": state,
            }
            (active if running else queued).append(item)
        return {
            "limits": asdict(self.scheduler.limits), "counts": self.scheduler.counts(),
            "queued": queued, "active": active,
        }
