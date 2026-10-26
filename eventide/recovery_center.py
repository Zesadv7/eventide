"""MK-08F readable recovery projection over committed Run facts (read-only).

Everything here is derived from one atomic ``store.recovery_snapshot`` cut plus
explicitly current scheduling facts the HTTP layer passes in. This module never
writes events, never runs models or tools, and never decides recovery on its
own: the Host owns claims and resumption. A Run whose admission predates the
frozen ``recovery_policy`` is reported as history without recovery evidence
instead of being guessed into a category.
"""

from __future__ import annotations

from typing import Any

from eventide.outcome_reconciliation import unresolved_outcomes
from eventide.projections import TERMINALS, RuntimeStateProjection

PROJECTION_VERSION = 1

BUDGET_STOP_REASONS = frozenset({"token_budget", "step_budget", "task_step_budget"})
HOST_PARK_REASONS = frozenset({
    "host_stopped_before_dispatch", "control_execution_not_auto_resumed",
    "unresolved_tool_protocol_pair", "pending_approval",
    "missing_post_tool_checkpoint", "no_frozen_recovery_policy",
    "workspace_evidence_changed", "superseded_by_user_run",
    "model_config_changed", "admission_not_persisted",
})
# Facts that make an interrupted Run not describable as safely resumable, so
# the projection must not advertise Continue even though the endpoint would
# re-check on its own. Budget parks stay manually continuable by existing
# semantics; there is deliberately no automatic retry action of any kind.
NOT_RESUMABLE_REASONS = frozenset({"unknown_tool_outcome"})


def recovery_successor_id(cut: dict[str, Any]) -> str | None:
    """The frozen successor identity from this Run's claim, if any."""
    for event in reversed(cut["events"]):
        if event["type"] == "recovery.claimed":
            claimed = event["payload"].get("resumed_as")
            return str(claimed) if claimed else None
    return None


def _terminal_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if event["type"] in TERMINALS:
            return event["payload"]
    return {}


def _classify_stop(
    status: str, events: list[dict[str, Any]], model_retry: dict[str, Any] | None,
) -> str:
    has_unknown = bool(unresolved_outcomes(events))
    if status in {"completed", "failed"}:
        return "completed" if status == "completed" else "failure"
    if status == "interrupted":
        payload = _terminal_payload(events)
        reason = payload.get("reason")
        if payload.get("recovery", {}).get("resumed_as"):
            return "auto_recovered"
        if reason in {"cancelled", "user_cancelled"}:
            return "user_cancel"
        if reason in BUDGET_STOP_REASONS:
            return "budget"
        if reason == "unknown_tool_outcome" or has_unknown:
            return "unknown_outcome"
        if reason == "repeated_tool_failure":
            return "repeated_failure"
        if reason in HOST_PARK_REASONS or reason is not None:
            return "host_restart"
        return "interrupted"
    if has_unknown:
        return "unknown_outcome"
    if model_retry is not None:
        return "model_retry_wait"
    if status == "waiting_for_user":
        return "waiting_user"
    return status


def build_recovery_view(
    cut: dict[str, Any], current: dict[str, Any],
) -> dict[str, Any]:
    """Project one Run's recovery facts from an atomic cut.

    ``current`` carries explicitly non-historical scheduling facts: whether the
    session is busy right now, whether it is a control session, the session's
    latest run id, and the successor's current summary when one exists.
    """
    identity, events = cut["identity"], cut["events"]
    run_id = identity["id"]
    state = RuntimeStateProjection.project(events)
    status = state["status"]
    terminal = any(event["type"] in TERMINALS for event in events)

    admitted: dict[str, Any] = next(
        (event["payload"] for event in events if event["type"] == "run.admitted"), {},
    )
    frozen_policy = admitted.get("recovery_policy")
    history_available = isinstance(frozen_policy, dict)

    claims = [
        {
            "resumed_as": event["payload"].get("resumed_as"),
            "verdict": event["payload"].get("verdict"),
            "at": event["ts"],
        }
        for event in events
        if event["type"] == "recovery.claimed"
    ]
    successor_run_id = recovery_successor_id(cut)
    blocked = [
        {
            "reason": event["payload"].get("reason"),
            "resumed_as": event["payload"].get("resumed_as"),
            "at": event["ts"],
        }
        for event in cut["blocked_facts"]
    ]
    recovery_of = admitted.get("recovery_of")

    unknown_tools = [
        {
            "call_id": event["payload"].get("call_id"),
            "name": event["payload"].get("name"),
            "error": event["payload"].get("error"),
            "at": event["ts"],
        }
        for event in unresolved_outcomes(events)
    ]
    pending_approvals = [
        {"approval_id": approval_id, "summary": payload.get("summary")}
        for approval_id, payload in state["pending_approvals"].items()
    ]

    checks = [{**event["payload"], "at": event["ts"]} for event in events
              if event["type"] == "tool.outcome_reconciled"]
    reconciled = any(check["status"] == "verified_after" for check in checks)
    terminal_payload = _terminal_payload(events)
    stop = {
        "kind": _classify_stop(status, events, state["model_retry"]),
        "reason": terminal_payload.get("reason") or state.get("reason"),
        "error": state.get("error"),
    }

    if not unknown_tools and any(e["type"] == "run.cancel_requested" for e in events):
        stop["kind"] = "user_cancel"
    elif blocked:
        stop["kind"] = "recovery_blocked"
    elif claims:
        stop["kind"] = "auto_recovered"
    elif reconciled and not unknown_tools and stop["reason"] == "unknown_tool_outcome":
        stop["kind"] = "file_outcome_verified"
    elif stop["reason"] == "service_shutdown":
        stop["kind"] = "service_shutdown"

    latest_run_id = current.get("latest_run_id")
    session_ready = not any(current.get(key) for key in (
        "session_busy", "control_session", "worker_session",
    ))
    interrupted_resumable = (
        status == "interrupted"
        and not unknown_tools
        and (stop["reason"] not in NOT_RESUMABLE_REASONS or reconciled)
    )
    stop_action = {
        "available": not terminal and not current.get("control_session"),
        "method": "POST",
        "path": f"/api/runs/{run_id}/cancel",
        "reason": "" if not terminal else "run_already_terminal",
    }
    continue_action = {
        "available": bool(
            session_ready and interrupted_resumable and latest_run_id == run_id
        ),
        "method": "POST",
        "path": f"/api/sessions/{identity['session_id']}/continue",
        "reason": (
            ""
            if session_ready and interrupted_resumable and latest_run_id == run_id
            else (
                "unknown_tool_outcome_requires_manual_review" if unknown_tools
                else "not_an_interrupted_latest_run"
            )
        ),
    }

    notes: list[str] = []
    if not history_available:
        notes.append(
            "admission_predates_frozen_recovery_policy"
        )
    if len(claims) > 1:
        notes.append("multiple_recovery_claims_found")

    successor: dict[str, Any] | None = None
    if successor_run_id:
        summary = current.get("successor_summary")
        successor = {
            "run_id": successor_run_id,
            "admitted": bool(summary),
            "current": summary,
        }

    return {
        "projection_version": PROJECTION_VERSION,
        "run_id": run_id,
        "session_id": identity["session_id"],
        "status": status,
        "is_terminal": terminal,
        "high_water": cut["high_water"],
        "continuation_of": identity.get("continuation_of") or recovery_of,
        "summary": {
            "steps": state["steps"],
            "tool_calls": state["tool_calls"],
            "checkpoint_seq": state["checkpoint"],
            "usage": state["usage"],
        },
        "model_retry": state["model_retry"],
        "stop": stop,
        "unknown_tools": unknown_tools,
        "outcome_checks": checks,
        "pending_approvals": pending_approvals,
        "recovery": {
            "frozen_policy": frozen_policy,
            "history_available": history_available,
            "recovery_of": recovery_of,
            "claims": claims,
            "successor_run_id": successor_run_id,
            "blocked": blocked,
            "successor": successor,
        },
        "available_actions": {"stop": stop_action, "continue": continue_action},
        "notes": notes,
    }
