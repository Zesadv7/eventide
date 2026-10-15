"""Atomic Token reservations derived exclusively from RuntimeStore facts.

This is application budget control, not a provider billing hard limit. Estimates
and unknown attempts remain visible; neither can masquerade as zero usage.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from eventide.store import RuntimeStore

ScopeKind = Literal["run", "delivery", "project", "day"]
BUDGET_FACTS = (
    "budget.configured", "budget.reserved", "budget.settled", "budget.resolved",
)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"Invalid {name}")
    return value


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def utc_time(at: datetime | None = None) -> datetime:
    value = at or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An explicit UTC offset is required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class BudgetScope:
    kind: ScopeKind
    identity: str

    def __post_init__(self) -> None:
        if self.kind not in {"run", "delivery", "project", "day"}:
            raise ValueError("Unknown budget scope")
        _text(self.identity, "scope identity")
        if self.kind == "day":
            try:
                parsed = datetime.strptime(self.identity, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("Day budget identity must be a UTC YYYY-MM-DD date") from exc
            if parsed.strftime("%Y-%m-%d") != self.identity:
                raise ValueError("Day budget identity must be a UTC YYYY-MM-DD date")


@dataclass(frozen=True)
class BudgetContext:
    project_id: str | None
    root_run_id: str
    delivery_id: str | None = None

    def __post_init__(self) -> None:
        if self.project_id is not None:
            _text(self.project_id, "project identity")
        _text(self.root_run_id, "root Run identity")
        if self.delivery_id is not None:
            _text(self.delivery_id, "delivery identity")

    def scopes(self, run_id: str, at: datetime) -> tuple[BudgetScope, ...]:
        scopes = [BudgetScope("run", self.root_run_id)]
        if run_id != self.root_run_id:
            scopes.append(BudgetScope("run", run_id))
        if self.delivery_id is not None:
            scopes.append(BudgetScope("delivery", self.delivery_id))
        if self.project_id is not None:
            scopes.append(BudgetScope("project", self.project_id))
        scopes.append(BudgetScope("day", utc_time(at).strftime("%Y-%m-%d")))
        return tuple(scopes)


def is_service_probe_run(
    store: RuntimeStore, run_id: str, events: list[dict[str, Any]] | None = None,
) -> bool:
    """Only a trusted, standalone connection-check Run can omit a project bucket."""
    identity = store.get_run_identity(run_id)
    if identity is None or identity["continuation_of"]:
        return False
    facts = events if events is not None else store.session_events(identity["session_id"])
    controls = [e["payload"] for e in facts
                if e["session_id"] == identity["session_id"] and e["type"] == "control.session"]
    runs = [e for e in facts if e["run_id"] == run_id]
    starts = [e["payload"] for e in runs if e["type"] == "run.started"]
    return (
        len(controls) == 1 and controls[0].get("kind") == "connection_probe"
        and len(starts) == 1 and starts[0].get("source") == "connection_probe"
        and starts[0].get("billing_scope") == "service"
        and starts[0].get("project_id") is None
        and not any(e["type"] == "run.admitted" for e in runs)
        and not any(e["payload"].get("parent_run_id") or e["payload"].get("delivery_id")
                    or e["payload"].get("execution_config", {}).get("writer_delivery_id")
                    for e in runs)
    )


class BudgetExceeded(ValueError):
    def __init__(self, violations: list[dict[str, Any]]):
        super().__init__("Model request would exceed its configured Token budget")
        self.violations = violations


@dataclass(frozen=True)
class ReservationReceipt:
    attempt_id: str
    run_id: str
    status: str
    reserved_tokens: int
    actual_tokens: int | None
    usage_source: str
    buckets: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _key(scope: dict[str, Any]) -> tuple[str, str]:
    return str(scope["kind"]), str(scope["identity"])


def _projection(events: list[dict[str, Any]]) -> dict[str, Any]:
    limits: dict[tuple[str, str], int | None] = {}
    requests: dict[str, dict[str, Any]] = {}
    reservations: dict[str, dict[str, Any]] = {}
    seen_changes: set[str] = set()
    for event in events:
        payload = event["payload"]
        event_type = event["type"]
        if event_type == "budget.configured":
            request_id = payload["request_id"]
            if request_id in requests and requests[request_id] != payload:
                raise ValueError("Conflicting budget configuration replay")
            if request_id in requests:
                continue
            requests[request_id] = payload
            limits[_key(payload["scope"])] = payload["token_limit"]
        elif event_type == "budget.reserved":
            attempt_id = payload["attempt_id"]
            prior = reservations.get(attempt_id)
            if prior and prior["reservation"] != payload:
                raise ValueError("Conflicting budget reservation replay")
            if prior is None:
                reservations[attempt_id] = {
                    "reservation": payload, "settlement": None, "resolution": None,
                }
        elif event_type in {"budget.settled", "budget.resolved"}:
            change = _change_key(event_type, payload)
            if change in seen_changes:
                continue
            seen_changes.add(change)
            item = reservations.get(payload["attempt_id"])
            if item is None:
                raise ValueError("Settlement has no reservation")
            field = "settlement" if event_type == "budget.settled" else "resolution"
            prior = item[field]
            if prior and prior != payload and (
                field != "settlement" or prior["actual_tokens"] is not None
                or payload["actual_tokens"] is None
            ):
                raise ValueError("Conflicting budget settlement replay")
            item[field] = payload
    return {
        "limits": limits, "requests": requests, "reservations": reservations,
        "seen_changes": seen_changes,
    }


def _change_key(event_type: str, payload: dict[str, Any]) -> str:
    return event_type + ":" + json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _amount(item: dict[str, Any]) -> tuple[int, int | None, str]:
    reservation = item["reservation"]
    settlement = item["settlement"]
    resolution = item["resolution"]
    if resolution:
        return 0, resolution["actual_tokens"], "manually_resolved"
    if settlement and settlement["actual_tokens"] is not None:
        return 0, settlement["actual_tokens"], "settled"
    observed = 0
    if settlement and settlement["usage_source"] == "reported":
        observed = sum(settlement["usage"].get(key, 0) for key in (
            "input_tokens", "output_tokens",
        ))
    return max(reservation["reserved_tokens"], observed), None, "unsettled"


def _bucket(state: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    key = _key(scope)
    actual = held = unsettled = 0
    for item in state["reservations"].values():
        if key not in {_key(value) for value in item["reservation"]["scopes"]}:
            continue
        reserved, known, _ = _amount(item)
        held += reserved
        actual += known or 0
        unsettled += int(known is None)
    limit = state["limits"].get(key)
    committed = actual + held
    return {
        **scope, "token_limit": limit, "actual_tokens": actual,
        "reserved_tokens": held, "committed_tokens": committed,
        "unsettled_attempts": unsettled,
        "remaining_tokens": max(limit - committed, 0) if limit is not None else None,
        "warning": limit is not None and committed * 5 >= limit * 4,
        "blocked": limit is not None and committed >= limit,
    }


def _receipt(state: dict[str, Any], attempt_id: str) -> ReservationReceipt:
    item = state["reservations"][attempt_id]
    reservation = item["reservation"]
    _, actual, status = _amount(item)
    settlement = item["settlement"]
    buckets = tuple(_bucket(state, scope) for scope in reservation["scopes"])
    return ReservationReceipt(
        attempt_id, reservation["run_id"], status, reservation["reserved_tokens"],
        actual, settlement["usage_source"] if settlement else "unknown", buckets,
        tuple(bucket for bucket in buckets if bucket["warning"]),
    )


class BudgetLedger:
    """One SQLite transaction per admission, settlement or audited adjustment."""

    def __init__(self, store: RuntimeStore):
        self.store = store

    def configure(
        self, session_id: str, *, scope: BudgetScope, token_limit: int | None,
        request_id: str,
    ) -> dict[str, Any]:
        if token_limit is not None:
            _count(token_limit, "token_limit")
        payload: dict[str, Any] = {
            "request_id": _text(request_id, "request identity"),
            "scope": asdict(scope), "token_limit": token_limit,
        }

        def decide(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[Any]]:
            state = _projection(events)
            prior = state["requests"].get(request_id)
            if prior and prior != payload:
                raise ValueError("Budget request identity was reused with different content")
            if prior:
                return _bucket(state, payload["scope"]), []
            state["limits"][_key(payload["scope"])] = token_limit
            return _bucket(state, payload["scope"]), [] if prior else [
                ("budget.configured", payload),
            ]

        return self.store.transact_facts(session_id, BUDGET_FACTS, decide, author="user")

    def reserve(
        self, session_id: str, run_id: str, attempt_id: str, *, context: BudgetContext,
        input_estimate: int, output_estimate: int, estimate_method: str,
        at: datetime | None = None,
        attempt_started: dict[str, Any] | None = None,
    ) -> ReservationReceipt:
        timestamp = utc_time(at)
        _text(attempt_id, "attempt identity")
        if attempt_started is not None and attempt_started.get("attempt_id") != attempt_id:
            raise ValueError("Model attempt identity mismatch")
        payload: dict[str, Any] = {
            "attempt_id": attempt_id, "run_id": _text(run_id, "Run identity"),
            "session_id": session_id, "context": asdict(context),
            "input_estimate": _count(input_estimate, "input estimate"),
            "output_estimate": _count(output_estimate, "output estimate"),
            "estimate_method": _text(estimate_method, "estimate method"),
            "reserved_tokens": input_estimate + output_estimate,
            "reserved_at": timestamp.isoformat(),
            "scopes": [asdict(scope) for scope in context.scopes(run_id, timestamp)],
        }

        def decide(events: list[dict[str, Any]]) -> tuple[ReservationReceipt, list[Any]]:
            state = _projection(events)
            prior = state["reservations"].get(attempt_id)
            if prior:
                original = prior["reservation"]
                # Time is assigned at first reservation, not by an HTTP replay.
                if any(original[key] != payload[key] for key in payload if key not in {
                    "reserved_at", "scopes",
                }):
                    raise ValueError("Attempt identity was reused with different content")
                if at is not None and original["reserved_at"] != payload["reserved_at"]:
                    raise ValueError("Attempt identity was reused with a different timestamp")
                return _receipt(state, attempt_id), []
            self._validate_context(session_id, run_id, context, events, state)
            violations = []
            for scope in payload["scopes"]:
                bucket = _bucket(state, scope)
                limit = bucket["token_limit"]
                if limit is not None and (
                    bucket["committed_tokens"] + payload["reserved_tokens"] > limit
                    or bucket["blocked"]
                ):
                    violations.append({**bucket, "requested_tokens": payload["reserved_tokens"]})
            if violations:
                raise BudgetExceeded(violations)
            state["reservations"][attempt_id] = {
                "reservation": payload, "settlement": None, "resolution": None,
            }
            additions = [("budget.reserved", payload)]
            if attempt_started is not None:
                additions.append(("model.attempt_started", attempt_started))
            return _receipt(state, attempt_id), additions

        return self.store.transact_facts(
            session_id, (*BUDGET_FACTS, "run.admitted", "run.started", "control.session"), decide,
            run_id=run_id if attempt_started is not None else None,
        )

    def _validate_context(
        self, session_id: str, run_id: str, context: BudgetContext,
        events: list[dict[str, Any]], state: dict[str, Any],
    ) -> None:
        identity = self.store.get_run_identity(run_id)
        if identity is None or identity["session_id"] != session_id:
            raise ValueError("Run/session identity mismatch")
        if context.project_id is None and (
            context.root_run_id != run_id or context.delivery_id is not None
            or not is_service_probe_run(self.store, run_id, events)
        ):
            raise ValueError("Only a standalone connection probe can omit project ownership")
        parents: dict[str, str] = {}
        for event in events:
            if event["type"] in {"run.admitted", "run.started"} and event["run_id"]:
                parent = event["payload"].get("parent_run_id")
                if parent:
                    if event["run_id"] in parents and parents[event["run_id"]] != parent:
                        raise ValueError("Conflicting parent Run identity")
                    parents[event["run_id"]] = parent
        ancestor = run_id
        visited = set()
        while ancestor in parents:
            if ancestor in visited:
                raise ValueError("Cyclic parent Run identity")
            visited.add(ancestor)
            ancestor = parents[ancestor]
            if self.store.get_run_identity(ancestor) is None:
                raise ValueError("Missing parent Run")
        if ancestor != context.root_run_id:
            raise ValueError("Budget root must be the actual root Run")
        for item in state["reservations"].values():
            old = item["reservation"]
            if old["context"]["root_run_id"] == context.root_run_id and (
                old["context"] != asdict(context)
            ):
                raise ValueError("A Run family cannot change budget ownership")

    def settle(
        self, session_id: str, attempt_id: str, usage: dict[str, Any] | None, *,
        source: Literal["reported", "estimated", "unknown"] = "reported",
        run_id: str | None = None,
        attempt_finished: dict[str, Any] | None = None,
    ) -> ReservationReceipt:
        if attempt_finished is not None and (
            run_id is None or attempt_finished.get("attempt_id") != attempt_id
        ):
            raise ValueError("Model attempt identity mismatch")
        if source not in {"reported", "estimated", "unknown"}:
            raise ValueError("Unknown usage source")
        observed = dict(usage or {})
        if source == "unknown" and observed:
            raise ValueError("Unknown usage cannot contain counters")
        for key, value in observed.items():
            _text(key, "usage category")
            _count(value, key)
        complete = source == "reported" and all(
            key in observed for key in ("input_tokens", "output_tokens")
        )
        payload = {
            "attempt_id": _text(attempt_id, "attempt identity"),
            "usage": observed, "usage_source": source if observed else "unknown",
            "actual_tokens": observed["input_tokens"] + observed["output_tokens"]
            if complete else None,
        }

        def decide(events: list[dict[str, Any]]) -> tuple[ReservationReceipt, list[Any]]:
            state = _projection(events)
            item = self._owned(state, session_id, attempt_id)
            if run_id is not None and item["reservation"]["run_id"] != run_id:
                raise ValueError("Attempt/Run identity mismatch")
            prior = item["settlement"]
            if _change_key("budget.settled", payload) in state["seen_changes"]:
                return _receipt(state, attempt_id), []
            if item["resolution"] or (prior and (
                prior["actual_tokens"] is not None or payload["actual_tokens"] is None
            )):
                raise ValueError("Conflicting attempt settlement")
            item["settlement"] = payload
            additions = [("budget.settled", payload)]
            if attempt_finished is not None:
                additions.append(("model.attempt_finished", attempt_finished))
            return _receipt(state, attempt_id), additions

        return self.store.transact_facts(session_id, BUDGET_FACTS, decide, run_id=run_id)

    @staticmethod
    def _owned(state: dict[str, Any], session_id: str, attempt_id: str) -> dict[str, Any]:
        item = state["reservations"].get(attempt_id)
        if item is None:
            raise ValueError("Unknown attempt reservation")
        if item["reservation"]["session_id"] != session_id:
            raise ValueError("Attempt/session identity mismatch")
        return item

    def resolve_unknown(
        self, session_id: str, attempt_id: str, *, actual_tokens: int,
        reason: str, request_id: str,
    ) -> ReservationReceipt:
        """Explicit human reconciliation, never an automatic zero-value release."""
        payload = {
            "attempt_id": _text(attempt_id, "attempt identity"),
            "actual_tokens": _count(actual_tokens, "actual_tokens"),
            "reason": _text(reason, "resolution reason"),
            "request_id": _text(request_id, "resolution request identity"),
        }

        def decide(events: list[dict[str, Any]]) -> tuple[ReservationReceipt, list[Any]]:
            state = _projection(events)
            item = self._owned(state, session_id, attempt_id)
            if item["resolution"] == payload:
                return _receipt(state, attempt_id), []
            if item["resolution"] or _amount(item)[1] is not None:
                raise ValueError("Attempt is already settled")
            if any(value["resolution"] and value["resolution"]["request_id"] == request_id
                   for value in state["reservations"].values()):
                raise ValueError("Resolution request identity was reused")
            item["resolution"] = payload
            return _receipt(state, attempt_id), [("budget.resolved", payload)]

        return self.store.transact_facts(session_id, BUDGET_FACTS, decide, author="user")

    def snapshot(self, *, scopes: tuple[BudgetScope, ...] = ()) -> dict[str, Any]:
        state = _projection(self.store.facts_by_type(BUDGET_FACTS))
        selected = [asdict(scope) for scope in scopes] if scopes else [
            {"kind": key[0], "identity": key[1]} for key in state["limits"]
        ]
        return {
            "budgets": [_bucket(state, scope) for scope in selected],
            "reservations": [
                _receipt(state, attempt).as_dict() for attempt in state["reservations"]
                if not scopes or any(
                    _key(scope) in {_key(value) for value in selected}
                    for scope in state["reservations"][attempt]["reservation"]["scopes"]
                )
            ],
        }
