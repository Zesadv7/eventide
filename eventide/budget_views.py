"""Read-only budget and cost views over the existing canonical facts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from eventide.budget_prices import summarize_cost_events
from eventide.budgets import BudgetScope
from eventide.usage import build_usage_ledger
from eventide.workspace import digest

if TYPE_CHECKING:
    from eventide.host import RuntimeHost

ViewKind = Literal["run", "session", "project", "delivery", "day"]
MODEL_FACTS = (
    "model.attempt_started", "model.attempt_finished", "model.response",
    "context.summary_generated",
)


def utc_day(value: str) -> str:
    return BudgetScope("day", value).identity


def validate_owner(host: RuntimeHost, kind: ViewKind, identity: str) -> None:
    if kind == "day":
        utc_day(identity)
        return
    if not identity or len(identity) > 256:
        raise ValueError("Invalid scope identity")
    if kind == "run":
        exists = host.store.get_run_identity(identity) is not None
    elif kind == "session":
        exists = host.store.get_session(identity) is not None
    elif kind == "project":
        exists = any(p.project_id == identity for p in host.store.list_projects())
    else:
        exists = any(e["type"] == "delivery.prepared"
                     for e in host.store.delivery_facts(identity))
    if not exists:
        raise LookupError(f"{kind.capitalize()} not found")


def control_owner(host: RuntimeHost) -> str:
    """Keep HTTP replay ownership stable when the startup Workspace changes."""
    for event in host.store.facts_by_type(("control.session",)):
        if event["payload"].get("kind") == "budgets":
            return str(event["session_id"])
    return host._budget_control_session()


def budget_view(host: RuntimeHost, scope: BudgetScope | None = None) -> dict[str, Any]:
    result = host.budget_status(scopes=(scope,) if scope is not None else ())
    events = host.store.facts_by_type((
        "budget.configured", "budget.reserved", "budget.resolved", "budget.unknown_acknowledged",
    ))
    for bucket in result["budgets"]:
        latest = next((e["payload"] for e in reversed(events)
                       if e["type"] == "budget.configured"
                       and e["payload"]["scope"] == {
                           "kind": bucket["kind"], "identity": bucket["identity"],
                       }), None)
        bucket["configuration_hash"] = digest(latest)
    reservations = {e["payload"]["attempt_id"]: e["payload"] for e in events
                    if e["type"] == "budget.reserved"}
    for attempt in result["reservations"]:
        saved = reservations[attempt["attempt_id"]]
        attempt.update({key: saved[key] for key in (
            "session_id", "context", "input_estimate", "output_estimate",
            "estimate_method", "reserved_at",
        )})
        attempt["audit"] = [
            {"type": e["type"], "at": e["ts"], "author": e["author"], **e["payload"]}
            for e in events if e["type"] in {"budget.resolved", "budget.unknown_acknowledged"}
            and e["payload"]["attempt_id"] == attempt["attempt_id"]
        ]
    return {"timezone": "UTC", **result}


def cost_view(
    host: RuntimeHost, *, kind: ViewKind | None = None, identity: str | None = None,
    include_children: bool = True, date_from: str | None = None, date_to: str | None = None,
) -> dict[str, Any]:
    if (kind is None) != (identity is None):
        raise ValueError("scope_kind and scope_id must be supplied together")
    if kind is not None and identity is not None:
        validate_owner(host, kind, identity)
    if date_from is not None:
        utc_day(date_from)
    if date_to is not None:
        utc_day(date_to)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("date_from must not be after date_to")
    events = host.store.facts_by_type((
        "budget.reserved", "cost.estimated", "run.admitted", "run.started", *MODEL_FACTS,
    ))
    reservations = {e["payload"]["attempt_id"]: e["payload"] for e in events
                    if e["type"] == "budget.reserved"}
    selected_runs = {identity} if kind == "run" else {
        r["id"] for r in host.store.list_runs(identity, limit=None)
    } if kind == "session" and identity is not None else set()
    if include_children and kind in {"run", "session"}:
        while True:
            descendants = {e["run_id"] for e in events
                           if e["type"] in {"run.admitted", "run.started"}
                           and e["payload"].get("parent_run_id") in selected_runs}
            if descendants.issubset(selected_runs):
                break
            selected_runs.update(descendants)

    def matches(run: str, project: str | None, delivery: str | None, day: str) -> bool:
        if date_from is not None and day < date_from:
            return False
        if date_to is not None and day > date_to:
            return False
        return (
            kind is None or kind in {"run", "session"} and run in selected_runs
            or kind == "project" and project == identity
            or kind == "delivery" and delivery == identity
            or kind == "day" and day == identity
        )

    selected: set[str] = set()
    for attempt_id, reservation in reservations.items():
        day = next(scope["identity"] for scope in reservation["scopes"] if scope["kind"] == "day")
        owner = reservation["context"]
        if matches(reservation["run_id"], owner["project_id"], owner["delivery_id"], day):
            selected.add(attempt_id)
    run_attribution: dict[str, tuple[str | None, str | None]] = {}
    parents: dict[str, str] = {}
    for event in events:
        if event["type"] not in {"run.admitted", "run.started"}:
            continue
        payload = event["payload"]
        previous = run_attribution.get(event["run_id"], (None, None))
        run_attribution[event["run_id"]] = (
            payload.get("project_id") or previous[0],
            payload.get("execution_config", {}).get("writer_delivery_id") or previous[1],
        )
        if payload.get("parent_run_id"):
            parents[event["run_id"]] = payload["parent_run_id"]

    def attribution(run_id: str) -> tuple[str | None, str | None]:
        project, delivery = run_attribution.get(run_id, (None, None))
        seen = {run_id}
        while run_id in parents:
            run_id = parents[run_id]
            if run_id in seen:
                raise ValueError("Conflicting Run ancestry")
            seen.add(run_id)
            inherited = run_attribution.get(run_id, (None, None))
            project, delivery = project or inherited[0], delivery or inherited[1]
        return project, delivery

    def include_call(call: dict[str, Any]) -> bool:
        attempt_id = call["attempt_id"]
        if attempt_id in reservations:
            owner = reservations[attempt_id]
            if owner["run_id"] != call["run_id"] or owner["session_id"] != call["session_id"]:
                raise ValueError("Conflicting attempt ownership")
            return attempt_id in selected
        project, delivery = attribution(call["run_id"])
        if project is None:
            session = host.store.get_session(call["session_id"])
            if session is not None:
                project = host.store.get_workspace(session["workspace_id"]).project_id
        day = datetime.fromtimestamp(call["started_at"], timezone.utc).strftime("%Y-%m-%d")
        return matches(call["run_id"], project, delivery, day)

    # Filter only after canonical attempt/legacy deduplication; filtering raw
    # events by date can otherwise count a response and its attempt twice.
    model_events = [e for e in events if e["type"] in MODEL_FACTS]
    workspaces = {}
    for event in model_events:
        session_id = event["session_id"]
        if session_id not in workspaces:
            session = host.store.get_session(session_id)
            workspaces[session_id] = session["workspace_id"] if session else None
        event["workspace_id"] = workspaces[session_id]
    usage = build_usage_ledger(model_events, call_filter=include_call)
    historical = sorted({call["attempt_id"] for call in usage["calls"]} - selected)
    costs = summarize_cost_events([
        e for e in events if e["type"] in {"budget.reserved", "cost.estimated"}
        and e["payload"]["attempt_id"] in selected
    ])
    return {
        **costs, "timezone": "UTC", "date_bounds": "inclusive",
        "scope": {"kind": kind, "identity": identity, "include_children": include_children,
                  "date_from": date_from, "date_to": date_to},
        "usage": usage, "attempt_count": len(selected) + len(historical),
        "historical_unreserved_attempt_ids": historical,
        "unknown_attempts": costs["unknown_attempts"] + len(historical),
        "complete": costs["complete"] and not historical,
    }
