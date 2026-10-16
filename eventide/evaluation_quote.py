"""Read-only planning estimates for an explicitly reviewed live batch."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any

from eventide.bundled_evaluations import load_builtin_suite
from eventide.eval_suite import Split
from eventide.evaluation_profiles import EvaluationPreset, EvaluationProfile, evaluation_profile
from eventide.workspace import digest

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


def evaluation_quote(
    host: RuntimeHost, project_id: str, *, suite_id: str = "engineering-v1",
    split: str = "development", preset: EvaluationPreset = "none",
    profile: EvaluationProfile | None = None,
) -> dict[str, Any]:
    host.store.get_project(project_id)
    if split not in {"development", "holdout", "all"}:
        raise ValueError("Invalid evaluation split")
    selected: Split | None = (
        "development" if split == "development" else "holdout" if split == "holdout" else None
    )
    suite = load_builtin_suite(suite_id, split=selected)
    profile = profile or evaluation_profile(preset)
    if profile.preset != preset:
        raise ValueError("Evaluation Skill preset does not match its profile")
    generation = host.generation_settings(host.settings.model)
    fact_response_format = (
        "json_object" if host.settings.provider == "openai_compatible"
        and "json_object" in generation["capabilities"]["response_formats"] else None
    )
    # A reproducible planning scenario, not a prediction or provider billing bound.
    total = len(suite["cases"]) * 20_000
    planning = {"input_tokens": total * 9 // 10, "output_tokens": total // 10}
    now = datetime.now(timezone.utc).isoformat()
    currencies: dict[str, dict[str, Any]] = {}
    for event in host.store.facts_by_type(("price.registered",)):
        value = event["payload"]
        if (value["provider"] != host.settings.provider or value["model"] != host.settings.model
                or value["effective_at"] > now
                or value["category"] not in planning):
            continue
        rates = currencies.setdefault(value["currency"], {})
        previous = rates.get(value["category"])
        if previous is None or value["effective_at"] > previous["effective_at"]:
            rates[value["category"]] = value
    estimates = {}
    for currency, rates in sorted(currencies.items()):
        lines = []
        with localcontext() as context:
            context.prec = 50
            for category, rate in sorted(rates.items()):
                amount = (Decimal(rate["price_per_million"]) * planning[category]
                          / Decimal(1_000_000))
                lines.append({
                    "category": category, "quantity": planning[category],
                    "price_version": rate["version_id"],
                    "price_per_million": rate["price_per_million"], "amount": str(amount),
                })
            amount = sum((Decimal(line["amount"]) for line in lines), Decimal(0))
        missing = sorted(set(planning) - set(rates))
        estimates[currency] = {
            "known_amount": str(amount), "complete": not missing,
            "unknown_categories": missing, "lines": lines,
        }
    value = {
        "project_id": project_id, "suite_id": suite_id, "suite_sha256": suite["suite_sha256"],
        "split": split, "case_count": len(suite["cases"]),
        "provider": host.settings.provider, "model": host.settings.model,
        "generation": generation, "planning_tokens": planning,
        "fact_response_format": fact_response_format,
        "profile": profile.snapshot(),
        "estimate_method": "observed_case_budget_90_10_v1",
        "assumptions": (
            "每题按20,000规划Token、输入90%/输出10%估算；不是预测或账单上限。"
            "不抵扣缓存，未包含最后一次请求超限、重试及未知消费的额外费用。"
        ),
        "estimates": estimates,
        "complete": bool(estimates) and all(item["complete"] for item in estimates.values()),
        "limits": {"steps": 12, "observed_tokens": 20_000, "case_seconds": 300},
    }
    return {**value, "quote_hash": digest(value)}
