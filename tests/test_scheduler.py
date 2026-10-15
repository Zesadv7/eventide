"""Scheduling ownership is bounded and never replaces the canonical event log."""

import asyncio
from dataclasses import replace

import pytest

from eventide.scheduler import (
    AdmissionTicket,
    QueueFull,
    RootLease,
    RuntimeScheduler,
    SchedulingLimits,
    project_queue,
)
from eventide.store import RuntimeStore


@pytest.fixture
def scheduler(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "schedule.sqlite")
    store.register_workspace(isolated_workspace, None)
    value = RuntimeScheduler(store)
    yield value
    store.close()


def session(scheduler, name):
    scheduler.store.create_session(name)
    return name


def admit(scheduler, run_id, session_id, **intent):
    scheduler.store.admit_run(run_id, session_id, [
        ("run.admitted", {"project_id": "project", **intent}, "system", "user"),
    ])


def accepted(scheduler, name, *, startable=True):
    sid = session(scheduler, f"session-{name}")
    ticket = scheduler.reserve_admission(name, sid, startable=startable)
    admit(scheduler, name, sid)
    return scheduler.commit_admission(ticket)


def terminate(scheduler, name):
    scheduler.store.append_event(name, "run.completed", {"output": "done"})


@pytest.mark.parametrize("field,value", [
    ("active_roots", 0), ("active_roots", True), ("model_requests", 0),
    ("model_requests", 1.5), ("waiting_roots", -1), ("waiting_roots", "20"),
])
def test_limits_are_explicit_validated_counts(field, value):
    with pytest.raises(ValueError):
        replace(SchedulingLimits(), **{field: value})
    assert SchedulingLimits(waiting_roots=0).waiting_roots == 0


def test_four_active_twenty_waiting_and_exact_rejection_boundary(scheduler):
    leases = [accepted(scheduler, f"run-{index}") for index in range(24)]
    assert all(lease is not None for lease in leases[:4])
    assert leases[4:] == [None] * 20
    assert scheduler.counts()["active_roots"] == 4
    assert scheduler.counts()["waiting_roots"] == 20
    assert all(entry.status == "queued" for entry in scheduler.entries())  # Before run.started.
    sid = session(scheduler, "overflow")
    with pytest.raises(QueueFull):
        scheduler.reserve_admission("overflow", sid, startable=True)
    assert scheduler.store.get_run_identity("overflow") is None
    assert scheduler.store.session_events(sid) == []
    assert scheduler.claim_next(lambda _entry: True) is None
    terminate(scheduler, "run-0")
    # A terminal does not release the real owner until Host cleanup finishes.
    assert scheduler.counts()["active_roots"] == 4
    assert scheduler.release_root(leases[0]) is True
    next_lease = scheduler.claim_next(lambda _entry: True)
    assert next_lease.run_id == "run-4"
    assert scheduler.counts()["active_roots"] == 4
    assert scheduler.counts()["waiting_roots"] == 19


def test_pre_admission_tickets_reserve_capacity_without_persisting_acceptance(scheduler):
    tickets = [scheduler.reserve_admission(
        f"run-{index}", session(scheduler, f"s-{index}"), startable=True,
    ) for index in range(24)]
    assert scheduler.counts()["preparing_admissions"] == 24
    assert scheduler.counts()["active_roots"] == 4
    assert scheduler.counts()["waiting_roots"] == 20
    assert scheduler.entries() == ()
    assert scheduler.store.facts_by_type(("run.admitted",)) == []
    with pytest.raises(QueueFull):
        scheduler.reserve_admission("full", session(scheduler, "full"), startable=True)
    assert scheduler.release_ticket(tickets[4]) is True
    assert scheduler.release_ticket(tickets[4]) is False
    assert scheduler.counts()["waiting_roots"] == 19
    for ticket in tickets:
        scheduler.release_ticket(ticket)
    assert scheduler.counts()["active_roots"] == scheduler.counts()["waiting_roots"] == 0


def test_unavailable_execution_resource_does_not_consume_root_capacity(scheduler):
    for index in range(20):
        assert accepted(scheduler, f"waiting-{index}", startable=False) is None
    assert scheduler.counts()["active_roots"] == 0
    with pytest.raises(QueueFull):
        scheduler.reserve_admission("blocked", session(scheduler, "blocked"), startable=False)
    assert scheduler.claim_next(lambda _entry: False) is None
    claimed = scheduler.claim_next(lambda entry: entry.run_id == "waiting-2")
    assert claimed.run_id == "waiting-2"
    assert scheduler.counts()["active_roots"] == 1
    assert scheduler.counts()["waiting_roots"] == 19


def test_session_identity_is_owned_through_preparation_execution_and_cleanup(scheduler):
    sid = session(scheduler, "same-session")
    ticket = scheduler.reserve_admission("first", sid, startable=True)
    with pytest.raises(ValueError, match="pending work"):
        scheduler.reserve_admission("second", sid, startable=True)
    with pytest.raises(ValueError, match="unstarted root"):
        scheduler.commit_admission(ticket)
    admit(scheduler, "first", sid)
    with pytest.raises(ValueError, match="committed or terminated"):
        scheduler.release_ticket(ticket)
    lease = scheduler.commit_admission(ticket)
    assert scheduler.counts()["active_roots"] == 1
    scheduler.store.append_event("first", "run.started", {})
    assert scheduler.counts()["active_roots"] == 1  # Fact + lease are not two roots.
    with pytest.raises(ValueError, match="terminal"):
        scheduler.release_root(lease)
    terminate(scheduler, "first")
    with pytest.raises(ValueError, match="pending work"):
        scheduler.reserve_admission("second", sid, startable=True)
    assert scheduler.release_root(lease) is True
    assert scheduler.release_root(lease) is False
    assert scheduler.reserve_admission("second", sid, startable=True)


def test_forged_or_other_scheduler_handles_cannot_release_an_owner(scheduler):
    ticket = scheduler.reserve_admission("run", session(scheduler, "s"), startable=True)
    forged = AdmissionTicket(ticket.run_id, ticket.session_id, ticket.lane)
    assert scheduler.release_ticket(forged) is False
    with pytest.raises(ValueError, match="not owned"):
        scheduler.commit_admission(forged)
    admit(scheduler, "run", "s")
    lease = scheduler.commit_admission(ticket)
    terminate(scheduler, "run")
    assert scheduler.release_root(RootLease("run", "s")) is False
    assert RuntimeScheduler(scheduler.store).release_root(lease) is False
    assert scheduler.release_root(lease) is True


def test_admission_commit_requires_matching_durable_identity(scheduler):
    ticket = scheduler.reserve_admission("run", session(scheduler, "owner"), startable=True)
    admit(scheduler, "run", session(scheduler, "other"))
    with pytest.raises(ValueError, match="Matching"):
        scheduler.commit_admission(ticket)
    terminate(scheduler, "run")
    assert scheduler.release_ticket(ticket) is True
    with pytest.raises(ValueError, match="already used"):
        scheduler.reserve_admission("run", "owner", startable=True)
    with pytest.raises(ValueError, match="existing Session"):
        scheduler.reserve_admission("missing", "missing", startable=True)


def test_queue_rebuild_uses_commit_order_and_excludes_workers_controls(scheduler, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("eventide.store.time.time", lambda: 200)
        assert accepted(scheduler, "first", startable=False) is None
    with monkeypatch.context() as patch:
        patch.setattr("eventide.store.time.time", lambda: 100)
        assert accepted(scheduler, "second", startable=False) is None
    admit(scheduler, "worker", session(scheduler, "worker"), parent_run_id="first")
    control_session = session(scheduler, "control")
    scheduler.store.create_run("control", control_session)
    scheduler.store.append_event("control", "run.started", {"source": "delivery"})
    before = scheduler.entries()
    assert [entry.run_id for entry in before] == ["first", "second"]
    assert [entry.admitted_at for entry in before] == [200, 100]
    path = scheduler.store.path
    scheduler.store.close()
    scheduler.store = RuntimeStore(path)
    restored = RuntimeScheduler(scheduler.store)
    assert restored.entries() == before
    assert restored.counts()["waiting_roots"] == 2
    assert scheduler.store.get_run("first")["status"] == "queued"
    # Rebuild never starts/replays work or rewrites canonical facts.
    assert scheduler.store.facts_by_type(("run.started",))[0]["run_id"] == "control"
    assert len(scheduler.store.facts_by_type(("run.started",))) == 1


def test_projection_deduplicates_identical_admission_and_keeps_first_terminal(scheduler):
    accepted(scheduler, "run", startable=False)
    event = scheduler.store.facts_by_type(("run.admitted",))[0]
    end = {**event, "type": "run.interrupted", "payload": {}}
    later = {**event, "type": "run.completed", "payload": {}}
    assert len(project_queue([event, event])) == 1
    assert project_queue([event, end, later])[0].status == "interrupted"
    assert project_queue([{**event, "partial": True}]) == ()
    assert project_queue([{**event, "run_id": None}]) == ()
    with pytest.raises(ValueError, match="Conflicting"):
        project_queue([event, {**event, "session_id": "forged"}])


def test_cancelled_queue_entry_is_skipped_and_does_not_release_another_owner(scheduler):
    first = accepted(scheduler, "first")
    accepted(scheduler, "cancelled", startable=False)
    accepted(scheduler, "following", startable=False)
    scheduler.store.append_event("cancelled", "run.interrupted", {"reason": "cancelled"})
    next_lease = scheduler.claim_next(lambda _entry: True)
    assert next_lease.run_id == "following"
    assert scheduler.counts()["active_roots"] == 2
    assert scheduler.counts()["waiting_roots"] == 0
    assert scheduler.release_root(RootLease(first.run_id, first.session_id)) is False


def test_existing_started_runs_and_session_contention_are_not_claimed_twice(scheduler):
    sid = session(scheduler, "same")
    admit(scheduler, "started", sid)
    scheduler.store.append_event("started", "run.started", {})
    admit(scheduler, "queued", sid)
    assert scheduler.counts()["active_roots"] == 1
    assert scheduler.claim_next(lambda _entry: True) is None
    terminate(scheduler, "started")
    assert scheduler.claim_next(lambda _entry: True).run_id == "queued"


async def test_model_wait_cancellation_exception_and_owner_cancellation_release_slots(scheduler):
    release = asyncio.Event()
    both = asyncio.Event()
    entered = 0

    async def call():
        nonlocal entered
        async with scheduler.model_slot():
            entered += 1
            if entered == 2:
                both.set()
            await release.wait()

    first, second = asyncio.create_task(call()), asyncio.create_task(call())
    await asyncio.wait_for(both.wait(), 1)
    third = asyncio.create_task(call())
    await asyncio.sleep(0)
    assert scheduler.counts()["active_models"] == 2
    assert scheduler.counts()["waiting_models"] == 1
    third.cancel()
    with pytest.raises(asyncio.CancelledError):
        await third
    assert entered == 2
    assert scheduler.counts()["waiting_models"] == 0
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert scheduler.counts()["active_models"] == 1
    with pytest.raises(RuntimeError):
        async with scheduler.model_slot():
            raise RuntimeError("attempt failed")
    assert scheduler.counts()["active_models"] == 1
    release.set()
    await second
    assert scheduler.counts()["active_models"] == 0
    assert scheduler.store.facts_by_type(("model.attempt_started", "budget.reserved")) == []


async def test_four_parent_owners_can_wait_for_workers_without_extra_root_claims(scheduler):
    leases = [accepted(scheduler, f"parent-{index}") for index in range(4)]
    accepted(scheduler, "queued", startable=False)
    peak = 0

    async def worker():
        nonlocal peak
        async with scheduler.model_slot():
            peak = max(peak, scheduler.counts()["active_models"])
            await asyncio.sleep(.001)

    async def parent(lease):
        async with scheduler.model_slot():
            await asyncio.sleep(.001)
        await asyncio.gather(worker(), worker())  # Outside the parent's model slot.
        assert scheduler.counts()["active_roots"] == 4
        terminate(scheduler, lease.run_id)

    await asyncio.wait_for(asyncio.gather(*(parent(lease) for lease in leases)), 2)
    assert peak == 2
    assert scheduler.claim_next(lambda _entry: True) is None  # Cleanup still owns roots.
    for lease in leases:
        scheduler.release_root(lease)
    assert scheduler.claim_next(lambda _entry: True).run_id == "queued"


async def test_retry_backoff_can_release_model_capacity(scheduler):
    scheduler = RuntimeScheduler(scheduler.store, SchedulingLimits(model_requests=1))
    backoff, resume = asyncio.Event(), asyncio.Event()
    order = []

    async def retrying():
        try:
            async with scheduler.model_slot():
                order.append("first")
                raise OSError("retryable failure")
        except OSError:
            backoff.set()
            await resume.wait()
        async with scheduler.model_slot():
            order.append("retry")

    task = asyncio.create_task(retrying())
    await asyncio.wait_for(backoff.wait(), 1)
    async with scheduler.model_slot():
        order.append("other")
    resume.set()
    await task
    assert order == ["first", "other", "retry"]
