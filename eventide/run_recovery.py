"""MK-08C persistent recovery classification over committed Run facts.

Pure projection: this module never schedules work or writes events. The Host
owns the recovery claim transaction and the restart wiring; this module only
decides which unfinished Runs may be auto-resumed after a Host restart and
which must stay parked for the user. A Run is only resumable when its
admission froze ``recovery_policy.auto_resume`` and every committed fact still
describes a safe boundary: no unknown tool outcomes, no unresolved tool
protocol pairs, no pending approvals, and no side effect missing its
post-tool checkpoint.

A Run that a *graceful* service shutdown finalized as recoverable goes
through the same safety boundary (``classify_shutdown_recovery``); only the
terminal fact it starts from differs. A durable user stop
(``run.cancel_requested``) beats service shutdown regardless of arrival
order, so a stop racing a close can never be disguised as recoverable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eventide.projections import TERMINALS

if TYPE_CHECKING:
    from eventide.store import RuntimeStore

RESUME = "resume"
FINALIZE = "finalize"

# Executions that own resources beyond the source workspace (sandboxes,
# deliveries, evaluations, indexes, control operations) are never auto-resumed
# by this slice; their lifecycle owners must settle them explicitly.
CONTROL_BINDING_KINDS = frozenset({
    "sandbox", "delivery", "evaluation", "index",
    "worktree_control", "history_import", "python_experiment",
})


def frozen_recovery_policy(intent: dict[str, Any] | None) -> dict[str, Any] | None:
    """The admission-frozen policy, or None for pre-MK-08C history (never upgraded)."""
    policy = (intent or {}).get("recovery_policy")
    if isinstance(policy, dict) and policy.get("auto_resume") is True:
        return policy
    return None


def _control_run(store: RuntimeStore, run_id: str, events: list[dict[str, Any]]) -> bool:
    from eventide.budgets import is_service_probe_run
    from eventide.delivery_control import is_delivery_control_run
    from eventide.evaluation_control import is_evaluation_run
    from eventide.history_import_control import is_history_import_run
    from eventide.index_control import is_index_run
    from eventide.worktree_control import is_worktree_control_run

    return bool(
        is_service_probe_run(store, run_id, events)
        or is_evaluation_run(store, run_id)
        or is_index_run(store, run_id)
        or is_delivery_control_run(store, run_id)
        or is_worktree_control_run(store, run_id)
        or is_history_import_run(store, run_id)
    )


def unresolved_tool_pairs(store: RuntimeStore, session_id: str) -> bool:
    """True when the crashed model step left tool_use blocks without results."""
    pending: set[str] = set()
    for event in store.session_events(session_id):
        if event["partial"]:
            continue
        kind, payload = event["type"], event["payload"]
        if kind == "model.response":
            for block in payload.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    pending.add(str(block.get("id")))
        elif kind in {"tool.completed", "tool.abandoned"}:
            pending.discard(str(payload.get("call_id")))
    return bool(pending)


def pending_approvals(store: RuntimeStore, session_id: str) -> bool:
    """True when an approval was requested and never granted, denied or closed."""
    open_ids: set[str] = set()
    for event in store.session_events(session_id):
        if event["partial"]:
            continue
        kind, payload = event["type"], event["payload"]
        if kind == "approval.required":
            open_ids.add(str(payload.get("approval_id")))
        elif kind.startswith("approval.") and payload.get("approval_id"):
            open_ids.discard(str(payload["approval_id"]))
    return bool(open_ids)


def user_cancel_requested(store: RuntimeStore, session_id: str, run_id: str) -> bool:
    """Durable user stop beats service shutdown, whatever order they arrived in."""
    return any(
        event["type"] == "run.cancel_requested"
        and event["payload"].get("run_id") == run_id
        for event in store.session_events(session_id)
        if not event["partial"]
    )


def classify_recovery_run(
    store: RuntimeStore, run_id: str,
) -> tuple[str, str]:
    """Verdict for one unfinished Run: RESUME only from a committed safe boundary."""
    identity = store.get_run_identity(run_id)
    if identity is None or store.run_is_terminal(run_id):
        return FINALIZE, "run_already_terminal"
    events = store.run_events(run_id)
    intents = [event["payload"] for event in events if event["type"] == "run.admitted"]
    if not intents:
        return FINALIZE, "admission_not_persisted"
    return _safe_boundary(store, run_id, identity, events, intents)


def classify_shutdown_recovery(
    store: RuntimeStore, run_id: str,
) -> tuple[str, str]:
    """Verdict for a Run a graceful shutdown finalized as recoverable.

    The original terminal fact must be exactly ``run.interrupted`` with
    ``reason=service_shutdown`` and ``resumable=true``; everything else —
    including every crash-park safety rule and a durable user stop — parks the
    Run with its terminal untouched.
    """
    identity = store.get_run_identity(run_id)
    if identity is None:
        return FINALIZE, "admission_not_persisted"
    events = store.run_events(run_id)
    terminals = [event for event in events if event["type"] in TERMINALS]
    if len(terminals) != 1 or terminals[0]["type"] != "run.interrupted":
        return FINALIZE, "terminal_not_shutdown_recoverable"
    payload = terminals[0]["payload"]
    if payload.get("reason") != "service_shutdown" or payload.get("resumable") is not True:
        return FINALIZE, "terminal_not_shutdown_recoverable"
    intents = [event["payload"] for event in events if event["type"] == "run.admitted"]
    if not intents:
        return FINALIZE, "admission_not_persisted"
    return _safe_boundary(store, run_id, identity, events, intents)


def _safe_boundary(
    store: RuntimeStore,
    run_id: str,
    identity: dict[str, Any],
    events: list[dict[str, Any]],
    intents: list[dict[str, Any]],
) -> tuple[str, str]:
    """Shared crash/shutdown safety rules over one Run's committed facts."""
    if user_cancel_requested(store, identity["session_id"], run_id):
        return FINALIZE, "user_cancelled"
    binding = next(
        (
            event["payload"]["execution_binding"]
            for event in events
            if event["type"] in {"run.admitted", "run.started"}
            and "execution_binding" in event["payload"]
        ),
        None,
    )
    if (binding and binding.get("kind") in CONTROL_BINDING_KINDS) or _control_run(
        store, run_id, events
    ):
        return FINALIZE, "control_execution_not_auto_resumed"
    if frozen_recovery_policy(intents[0]) is None:
        return FINALIZE, "no_frozen_recovery_policy"
    if any(event["type"] == "tool.outcome_unknown" for event in events):
        return FINALIZE, "unknown_tool_outcome"
    if unresolved_tool_pairs(store, identity["session_id"]):
        return FINALIZE, "unresolved_tool_protocol_pair"
    if pending_approvals(store, identity["session_id"]):
        return FINALIZE, "pending_approval"
    last_checkpoint = max(
        (
            event["session_seq"]
            for event in events
            if event["type"] == "workspace.checkpoint"
        ),
        default=0,
    )
    readonly_calls = {
        event["payload"]["call_id"]
        for event in events
        if event["type"] == "tool.prepared" and event["payload"].get("readonly", False)
    }
    for event in events:
        if (
            event["type"] == "tool.completed"
            and event["session_seq"] > last_checkpoint
            and event["payload"]["call_id"] not in readonly_calls
        ):
            return FINALIZE, "missing_post_tool_checkpoint"
    for event in events:
        if event["type"] == "tool.prepared" and not event["payload"].get("readonly", False):
            call_id = event["payload"]["call_id"]
            if not any(
                follow["type"] in {"tool.completed", "tool.abandoned", "tool.outcome_unknown"}
                and follow["payload"].get("call_id") == call_id
                for follow in events
            ):
                return FINALIZE, "unresolved_prepared_tool"
    return RESUME, "safe_committed_boundary"
