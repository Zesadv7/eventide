"""Persistent delivery limits apply to every attributed provider attempt, including workers."""

from __future__ import annotations

import time
from typing import Any

from eventide.budgets import BudgetExceeded
from eventide.store import RuntimeStore
from eventide.usage import build_usage_ledger

MODEL_FACTS = ("model.attempt_started", "model.attempt_finished")


def delivery_usage(store: RuntimeStore, delivery_id: str) -> dict[str, Any]:
    facts = store.facts_by_type(("budget.reserved", *MODEL_FACTS))
    attempts = {e["payload"]["attempt_id"] for e in facts if e["type"] == "budget.reserved"
                and e["payload"]["context"].get("delivery_id") == delivery_id}
    return build_usage_ledger([e for e in facts if e["type"] in MODEL_FACTS
                               and e["payload"].get("attempt_id") in attempts])


def delivery_limit_reason(
    store: RuntimeStore, delivery_id: str, *, kind: str = "agent", admission: bool = True,
) -> str | None:
    facts = store.delivery_facts(delivery_id)
    started = next((e["payload"] for e in facts if e["type"] == "delivery.repair_started"), None)
    if started is None:
        return None
    if admission and any(e["type"] == "delivery.repair_finished" for e in facts):
        return "delivery_closed"
    owner = store.get_run_summary(started["control_run_id"])
    if admission and (owner is None or owner["status"] not in {"running", "queued"}):
        return "delivery_owner_stopped"
    if time.time() >= started["deadline"]:
        return "delivery_time_limit"
    ledger = delivery_usage(store, delivery_id)
    if sum(ledger["reported_totals"].get(key, 0) for key in (
        "input_tokens", "output_tokens",
    )) >= started["limits"]["observed_tokens"]:
        return "delivery_token_limit"
    calls = sum(call["kind"] != "embedding" for call in ledger["calls"])
    if admission and kind != "embedding" and calls >= started["limits"]["model_attempts"]:
        return "delivery_step_limit"
    return None


def enforce_delivery_request(store: RuntimeStore, delivery_id: str | None, kind: str) -> None:
    if delivery_id is None:
        return
    reason = delivery_limit_reason(store, delivery_id, kind=kind)
    if reason:
        raise BudgetExceeded([{"scope": {"kind": "delivery", "identity": delivery_id},
                               "reason": reason}])
