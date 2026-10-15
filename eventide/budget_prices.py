"""Versioned, currency-preserving fee estimates from observed request usage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from eventide.budgets import BUDGET_FACTS, _text, utc_time
from eventide.store import RuntimeStore
from eventide.usage import usage_total_fields

PRICE_FACTS = ("price.registered", "cost.estimated")
PRICE_CATEGORIES = frozenset({
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
})


@dataclass(frozen=True)
class PriceVersion:
    version_id: str
    provider: str
    model: str
    category: str
    currency: str
    price_per_million: str
    effective_at: datetime

    def payload(self) -> dict[str, Any]:
        for name in ("version_id", "provider", "model"):
            _text(getattr(self, name), name)
        if self.category not in PRICE_CATEGORIES:
            raise ValueError("Unsupported pricing category")
        if (not isinstance(self.currency, str) or len(self.currency) != 3
                or not self.currency.isascii() or not self.currency.isalpha()
                or not self.currency.isupper()):
            raise ValueError("Currency must be a three-letter uppercase code")
        if not isinstance(self.price_per_million, str) or len(self.price_per_million) > 64:
            raise ValueError("Price must be an exact decimal string")
        try:
            price = Decimal(self.price_per_million)
        except InvalidOperation as exc:
            raise ValueError("Price must be an exact decimal string") from exc
        if not price.is_finite() or price < 0 or price > Decimal("1e12"):
            raise ValueError("Price must be finite and nonnegative")
        exponent = price.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -18:
            raise ValueError("Price precision exceeds eighteen decimal places")
        return {
            "version_id": self.version_id, "provider": self.provider, "model": self.model,
            "category": self.category, "currency": self.currency,
            "price_per_million": str(price),
            "effective_at": utc_time(self.effective_at).isoformat(),
        }


def _price_lines(
    usage: dict[str, int], rates: dict[str, dict[str, Any]], *, basis: str = "input_output",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Partition subsets before pricing; never add cache/reasoning twice."""
    quantities: dict[str, int] = {}
    unknown = []
    totals = usage_total_fields(basis)
    for total, details in (
        ("input_tokens", ("cache_read_input_tokens", "cache_creation_input_tokens")),
        ("output_tokens", ("reasoning_tokens",)),
    ):
        if total not in totals:
            continue
        if total not in usage:
            unknown.append(total)
            continue
        quantity = usage[total]
        parts: dict[str, int] = {}
        for detail in details:
            if detail not in rates:
                continue
            if detail not in usage:
                unknown.append(detail)
                quantity = -1
                break
            parts[detail] = usage[detail]
            quantity -= usage[detail]
        if quantity < 0:
            unknown.append(total)
        else:
            quantities.update(parts)
            quantities[total] = quantity
    lines = []
    for category, quantity in quantities.items():
        rate = rates.get(category)
        if rate is None:
            unknown.append(category)
            continue
        with localcontext() as context:
            context.prec = 50
            amount = Decimal(rate["price_per_million"]) * quantity / Decimal(1_000_000)
        lines.append({
            "category": category, "quantity": quantity, "price_version": rate["version_id"],
            "price_per_million": rate["price_per_million"], "amount": str(amount),
        })
    return lines, sorted(set(unknown))


class CostLedger:
    """Persist fee snapshots; a new price never rewrites an earlier estimate."""

    def __init__(self, store: RuntimeStore):
        self.store = store

    def register_price(self, session_id: str, price: PriceVersion) -> dict[str, Any]:
        payload = price.payload()

        def decide(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[Any]]:
            for event in events:
                if event["type"] != "price.registered":
                    continue
                old = event["payload"]
                if old["version_id"] == payload["version_id"]:
                    if old != payload:
                        raise ValueError("Price version identity was reused")
                    return payload, []
                if all(old[key] == payload[key] for key in (
                    "provider", "model", "category", "currency", "effective_at",
                )):
                    raise ValueError("A price category already has a version at this time")
            return payload, [("price.registered", payload)]

        return self.store.transact_facts(session_id, PRICE_FACTS, decide, author="user")

    def estimate_cost(
        self, session_id: str, attempt_id: str, *, provider: str, model: str,
        estimate_id: str, reevaluation_of: str | None = None,
    ) -> dict[str, Any]:
        intent = {
            "attempt_id": _text(attempt_id, "attempt identity"),
            "provider": _text(provider, "provider"), "model": _text(model, "model"),
            "estimate_id": _text(estimate_id, "estimate identity"),
            "reevaluation_of": reevaluation_of,
        }

        def decide(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[Any]]:
            reservation = next((event["payload"] for event in events
                                if event["type"] == "budget.reserved"
                                and event["payload"]["attempt_id"] == attempt_id), None)
            if reservation is None:
                raise ValueError("Unknown attempt reservation")
            if reservation["session_id"] != session_id:
                raise ValueError("Attempt/session identity mismatch")
            prior_estimates = [event["payload"] for event in events
                               if event["type"] == "cost.estimated"]
            for old in prior_estimates:
                if old["estimate_id"] == estimate_id:
                    if any(old[key] != value for key, value in intent.items()):
                        raise ValueError("Cost estimate identity was reused")
                    return old, []
            if reevaluation_of is None and any(
                old["attempt_id"] == attempt_id for old in prior_estimates
            ):
                raise ValueError("An existing cost estimate requires explicit reevaluation")
            if reevaluation_of is not None:
                previous = next((item for item in prior_estimates
                                 if item["estimate_id"] == reevaluation_of), None)
                if previous is None or any(previous[key] != intent[key] for key in (
                    "attempt_id", "provider", "model",
                )):
                    raise ValueError("Revaluation must reference an estimate of the same attempt")
            settlements = [event["payload"] for event in events
                           if event["type"] == "budget.settled"
                           and event["payload"]["attempt_id"] == attempt_id]
            settlement = settlements[-1] if settlements else None
            usage = settlement["usage"] if settlement else {}
            price_at = reservation["reserved_at"]
            currencies: dict[str, dict[str, dict[str, Any]]] = {}
            for event in events:
                price = event["payload"]
                if event["type"] != "price.registered" or any(
                    price[key] != intent[key] for key in ("provider", "model")
                ) or price["effective_at"] > price_at:
                    continue
                rates = currencies.setdefault(price["currency"], {})
                previous_price = rates.get(price["category"])
                if previous_price is None or price["effective_at"] > previous_price["effective_at"]:
                    rates[price["category"]] = price
            estimates = {}
            for currency, rates in currencies.items():
                lines, unknown = _price_lines(
                    usage, rates, basis=reservation.get("usage_basis", "input_output"),
                )
                with localcontext() as context:
                    context.prec = 50
                    amount = sum((Decimal(line["amount"]) for line in lines), Decimal(0))
                estimates[currency] = {
                    "known_amount": str(amount) if lines else None,
                    "complete": not unknown, "unknown_categories": unknown, "lines": lines,
                }
            result = {
                **intent, "session_id": session_id, "pricing_at": price_at,
                "usage_source": settlement["usage_source"] if settlement else "unknown",
                "estimates": estimates,
                "complete": bool(estimates) and all(
                    item["complete"] for item in estimates.values()
                ),
                "unknown_reason": "no_effective_price" if not estimates else None,
            }
            return result, [("cost.estimated", result)]

        return self.store.transact_facts(session_id, (*PRICE_FACTS, *BUDGET_FACTS), decide)

    def snapshot(self) -> dict[str, Any]:
        """Latest estimate per attempt; currencies and incomplete calls stay separate."""
        return summarize_cost_events(
            self.store.facts_by_type(("cost.estimated", "budget.reserved"))
        )


def summarize_cost_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild a scoped snapshot from caller-selected canonical attempt facts."""
    latest: dict[str, dict[str, Any]] = {}
    history = []
    attempts = set()
    for event in events:
        if event["type"] == "budget.reserved":
            attempts.add(event["payload"]["attempt_id"])
            continue
        estimate = event["payload"]
        history.append(estimate)
        latest[estimate["attempt_id"]] = estimate
    totals: dict[str, Decimal] = {}
    estimated_totals: dict[str, Decimal] = {}
    for estimate in latest.values():
        target = estimated_totals if estimate["usage_source"] == "estimated" else totals
        for currency, value in estimate["estimates"].items():
            if value["known_amount"] is not None:
                with localcontext() as context:
                    context.prec = 50
                    target[currency] = (
                        target.get(currency, Decimal(0)) + Decimal(value["known_amount"])
                    )
    unpriced = sorted(attempts - set(latest))
    return {
        "known_costs": {currency: str(value) for currency, value in totals.items()},
        "estimated_usage_costs": {
            currency: str(value) for currency, value in estimated_totals.items()
        },
        "estimated_attempts": sum(
            item["usage_source"] == "estimated" for item in latest.values()
        ),
        "unknown_attempts": len(unpriced) + sum(
            not item["complete"] for item in latest.values()
        ),
        "unpriced_attempt_ids": unpriced,
        "complete": bool(latest) and not unpriced and all(
            item["complete"] and item["usage_source"] == "reported"
            for item in latest.values()
        ),
        "latest": list(latest.values()), "history": history,
    }
