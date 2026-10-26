"""Single-process scheduling primitives over canonical Run facts.

No task is started and no event is written here. Host integration must hold a
root lease through execution cleanup, acquire execution resources before launch,
and recover unfinished Runs before admitting work after restart.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from eventide.models import ToolCall
from eventide.store import RuntimeStore

SCHEDULING_FACTS = (
    "run.admitted", "run.started", "run.completed", "run.failed", "run.interrupted",
)
TERMINAL_TYPES = frozenset({"run.completed", "run.failed", "run.interrupted"})


@dataclass(frozen=True, slots=True)
class SchedulingLimits:
    active_roots: int = 4
    model_requests: int = 2
    waiting_roots: int = 20
    readonly_tools: int = 4

    def __post_init__(self) -> None:
        for name in ("active_roots", "model_requests", "waiting_roots", "readonly_tools"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < (0 if name == "waiting_roots" else 1):
                raise ValueError(f"Invalid {name}")


@dataclass(frozen=True, slots=True)
class QueueEntry:
    run_id: str
    session_id: str
    project_id: str | None
    status: str
    admitted_at: float
    admission_event_id: str
    intent: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolBatch:
    """One dispatch group over the tool calls of a single model response."""

    parallel: bool
    calls: tuple[ToolCall, ...]


def plan_readonly_batches(
    calls: Sequence[ToolCall],
    handlers: Mapping[str, Callable[..., object]],
    eligible: Iterable[Callable[..., object]],
) -> tuple[ToolBatch, ...]:
    """Split one model response into consecutive dispatch groups.

    A group uses the shared read slots only when every call resolves to the
    exact trusted read-only implementation object.
    Eligibility is never the tool name, a declared flag or MCP metadata: a
    custom handler registered under the same name, a coroutine handler or any
    write/unknown/interactive call executes alone, and that singleton is the
    barrier between parallel groups. Handlers may be unhashable objects, so
    matching uses identity and never dict/set membership.
    """

    eligible_tuple = tuple(eligible)

    def trusted(call: ToolCall) -> bool:
        handler = handlers.get(call.name)
        return (
            handler is not None
            and any(handler is candidate for candidate in eligible_tuple)
            and not inspect.iscoroutinefunction(handler)
        )

    batches: list[ToolBatch] = []
    group: list[ToolCall] = []
    for call in calls:
        if not trusted(call):
            if group:
                batches.append(ToolBatch(True, tuple(group)))
                group = []
            batches.append(ToolBatch(False, (call,)))
        else:
            group.append(call)
    if group:
        batches.append(ToolBatch(True, tuple(group)))
    return tuple(batches)


def project_queue(events: Iterable[Mapping[str, Any]]) -> tuple[QueueEntry, ...]:
    """Use Store commit order, not wall-clock time or per-Session sequence.

    Only admitted roots belong to this queue. Read-only children and control Runs
    without admission are not extra root jobs. Historical events remain unchanged.
    """
    admissions: dict[str, Mapping[str, Any]] = {}
    starts: set[str] = set()
    terminals: dict[str, str] = {}
    for event in events:
        if event.get("partial"):
            continue
        kind = event["type"]
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if kind == "run.admitted":
            prior = admissions.get(run_id)
            if prior is not None and (
                prior["session_id"] != event["session_id"]
                or prior["payload"] != event["payload"]
            ):
                raise ValueError("Conflicting Run admission facts")
            admissions.setdefault(run_id, event)
        elif kind == "run.started":
            starts.add(run_id)
        elif kind in TERMINAL_TYPES:
            terminals.setdefault(run_id, kind.removeprefix("run."))
    return tuple(
        QueueEntry(
            run_id=run_id, session_id=event["session_id"],
            project_id=event["payload"].get("project_id"),
            status=terminals.get(run_id, "running" if run_id in starts else "queued"),
            admitted_at=event["ts"], admission_event_id=event["event_id"],
            intent=dict(event["payload"]),
        )
        for run_id, event in admissions.items()
        if not event["payload"].get("parent_run_id")
    )


class QueueFull(ValueError):
    """No acceptance capacity remains; transport must not create an orphan Run."""


@dataclass(frozen=True, slots=True)
class AdmissionTicket:
    run_id: str
    session_id: str
    lane: Literal["active", "waiting"]


@dataclass(frozen=True, slots=True)
class RootLease:
    run_id: str
    session_id: str


class RuntimeScheduler:
    """One Host/event-loop owner; tickets and leases are transient ownership only.

    An admission ticket reserves capacity while Host validates/freezes a request.
    It is not an accepted request: only Store.admit_run can make acceptance durable.
    Root leases include pre-start preparation and final cleanup. Model slots cover
    one actual request attempt, never a whole Run, retry backoff or child wait.
    """

    def __init__(self, store: RuntimeStore, limits: SchedulingLimits | None = None):
        self.store = store
        self.limits = limits or SchedulingLimits()
        self._tickets: dict[str, AdmissionTicket] = {}
        self._roots: dict[str, RootLease] = {}
        self._models = asyncio.BoundedSemaphore(self.limits.model_requests)
        self._active_models = 0
        self._waiting_models = 0
        self._readonly = asyncio.BoundedSemaphore(self.limits.readonly_tools)
        self._active_readonly = 0
        self._waiting_readonly = 0

    def entries(self) -> tuple[QueueEntry, ...]:
        return project_queue(self.store.facts_by_type(SCHEDULING_FACTS))

    def counts(self) -> dict[str, int]:
        entries = self.entries()
        owned = self._tickets.keys() | self._roots.keys()
        return {
            "active_roots": len(self._roots)
            + sum(ticket.lane == "active" for ticket in self._tickets.values())
            + sum(entry.status == "running" and entry.run_id not in owned for entry in entries),
            "waiting_roots": sum(ticket.lane == "waiting" for ticket in self._tickets.values())
            + sum(entry.status == "queued" and entry.run_id not in owned for entry in entries),
            "preparing_admissions": len(self._tickets),
            "active_models": self._active_models,
            "waiting_models": self._waiting_models,
            "active_readonly_tools": self._active_readonly,
            "waiting_readonly_tools": self._waiting_readonly,
        }

    def reserve_admission(
        self, run_id: str, session_id: str, *, startable: bool,
    ) -> AdmissionTicket:
        """Reserve 4 active + 20 waiting capacity before any acceptance side effect.

        `startable` must be decided by Host's execution-resource admission checks.
        A busy source cannot be declared startable merely because a root slot is free.
        """
        if not run_id or not session_id or self.store.get_session(session_id) is None:
            raise ValueError("A Run identity and existing Session are required")
        if run_id in self._tickets or run_id in self._roots or self.store.get_run_identity(run_id):
            raise ValueError("Run identity already used")
        if (any(ticket.session_id == session_id for ticket in self._tickets.values())
                or any(lease.session_id == session_id for lease in self._roots.values())) or any(
            entry.session_id == session_id and entry.status in {"queued", "running"}
            for entry in self.entries()
        ):
            raise ValueError("Session is busy with pending work")
        counts = self.counts()
        lane: Literal["active", "waiting"] = "waiting"
        if startable and counts["active_roots"] < self.limits.active_roots:
            lane = "active"
        elif counts["waiting_roots"] >= self.limits.waiting_roots:
            raise QueueFull("Run queue is full")
        ticket = AdmissionTicket(run_id, session_id, lane)
        self._tickets[run_id] = ticket
        return ticket

    def commit_admission(self, ticket: AdmissionTicket) -> RootLease | None:
        """Transfer a ticket only after its exact root admission is durable."""
        if self._tickets.get(ticket.run_id) is not ticket:
            raise ValueError("Admission ticket is not owned by this scheduler")
        entry = next((item for item in self.entries() if item.run_id == ticket.run_id), None)
        if entry is None or entry.session_id != ticket.session_id or entry.status != "queued":
            raise ValueError("Matching unstarted root admission is required")
        lease = RootLease(ticket.run_id, ticket.session_id) if ticket.lane == "active" else None
        if lease is not None:
            self._roots[ticket.run_id] = lease
        self._tickets.pop(ticket.run_id)
        return lease

    def release_ticket(self, ticket: AdmissionTicket) -> bool:
        if self._tickets.get(ticket.run_id) is not ticket:
            return False
        if (self.store.get_run_identity(ticket.run_id) is not None
                and not self.store.run_is_terminal(ticket.run_id)):
            raise ValueError("Durable admission must be committed or terminated before release")
        self._tickets.pop(ticket.run_id)
        return True

    def claim_next(self, claim_execution: Callable[[QueueEntry], bool]) -> RootLease | None:
        """Claim the oldest eligible queued root without awaiting or holding a DB transaction.

        Host's synchronous callback must claim its actual execution key if it
        returns True, or leave resources untouched and return False. A root lease
        is not a Workspace/Session/Git lock and never substitutes for those checks.
        """
        if self.counts()["active_roots"] >= self.limits.active_roots:
            return None
        entries = self.entries()
        busy_sessions = (
            {entry.session_id for entry in entries if entry.status == "running"}
            | {lease.session_id for lease in self._roots.values()}
            | {ticket.session_id for ticket in self._tickets.values()}
        )
        for entry in entries:
            if (entry.status != "queued" or entry.run_id in self._tickets
                    or entry.run_id in self._roots or entry.session_id in busy_sessions):
                continue
            if claim_execution(entry):
                lease = RootLease(entry.run_id, entry.session_id)
                self._roots[entry.run_id] = lease
                return lease
        return None

    def release_root(self, lease: RootLease) -> bool:
        """Release after both the Run terminal and Host's actual cleanup are complete."""
        if self._roots.get(lease.run_id) is not lease:
            return False
        if not self.store.run_is_terminal(lease.run_id):
            raise ValueError("Root must reach a terminal fact before releasing its lease")
        self._roots.pop(lease.run_id)
        return True

    @asynccontextmanager
    async def model_slot(self) -> AsyncIterator[None]:
        self._waiting_models += 1
        try:
            await self._models.acquire()
        finally:
            self._waiting_models -= 1
        self._active_models += 1
        try:
            yield
        finally:
            self._active_models -= 1
            self._models.release()

    @asynccontextmanager
    async def readonly_slot(self) -> AsyncIterator[None]:
        """One service-wide trusted read-only tool execution (MK-11).

        The cap is shared by every Run of this Host, including workers and child
        copies. A cancelled wait never grants a slot, so a queued call that is
        cancelled before dispatch cannot start executing. The caller must keep
        the slot until the tool's actual work is finished: a synchronous file
        tool runs in a thread, and releasing early would break the ownership
        guarantee even though the coroutine already returned.
        """
        self._waiting_readonly += 1
        try:
            await self._readonly.acquire()
        finally:
            self._waiting_readonly -= 1
        self._active_readonly += 1
        try:
            yield
        finally:
            self._active_readonly -= 1
            self._readonly.release()
