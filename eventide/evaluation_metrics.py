"""Read-only experiment statistics from schema-v2 evaluation evidence.

These are disposable projections, never another usage ledger or a price engine.
Reported totals, price snapshots and attempt identities remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from eventide.generation import GenerationPatch
from eventide.usage import usage_snapshot, usage_total_fields

_TRUNCATED = {"length", "max_tokens", "max_output_tokens"}
_MISSING = {"missing": True}


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _number(value: Any) -> float | None:
    return (
        float(value)
        if type(value) in {int, float} and math.isfinite(value) and value >= 0
        else None
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _ratio(part: int, total: int) -> float | None:
    return part / total if total else None


def _latency(values: list[float], total: int) -> dict[str, Any]:
    ordered = sorted(values)

    def percentile(fraction: float) -> float | None:
        if not ordered:
            return None
        # Nearest-rank: finite observed samples, no invented interpolated request.
        return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]

    return {
        "mean_ms": sum(values) / len(values) if values else None,
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "observed": len(values),
        "total": total,
        "complete": len(values) == total,
    }


def _tokens(calls: list[dict[str, Any]], *, missing_cases: int = 0) -> dict[str, Any]:
    reported: Counter[str] = Counter()
    estimated: Counter[str] = Counter()
    for call in calls:
        if call["usage_source"] in {"reported", "estimated"}:
            target = reported if call["usage_source"] == "reported" else estimated
            target.update(call["usage"])
    complete = not missing_cases and all(call["usage_complete"] for call in calls)
    # Cache and reasoning are subsets and never additional total Token.
    total = (
        sum(
            sum(
                call["usage"][field]
                for field in usage_total_fields(
                    call["usage_basis"],
                )
            )
            for call in calls
        )
        if complete
        else None
    )
    return {
        "reported": dict(reported),
        "estimated": dict(estimated),
        "complete": complete,
        "total_tokens": total,
        "unknown_attempts": sum(c["usage_source"] == "unknown" for c in calls),
        "incomplete_attempts": sum(not c["usage_complete"] for c in calls),
        "missing_evidence_cases": missing_cases,
    }


def _amount(value: Any) -> Decimal:
    try:
        number = Decimal(value) if isinstance(value, str) else Decimal("NaN")
    except InvalidOperation as exc:
        raise ValueError("Cost amount must be a finite nonnegative decimal string") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("Cost amount must be a finite nonnegative decimal string")
    return number


def _costs(
    calls: list[dict[str, Any]],
    costs: dict[tuple[str, str], dict[str, Any]],
    *,
    missing_cases: int,
    successes: int,
) -> dict[str, Any]:
    keys = {(c["run_id"], c["attempt_id"]) for c in calls}
    costs = {key: cost for key, cost in costs.items() if key in keys}
    currencies = sorted({currency for cost in costs.values() for currency in cost["estimates"]})
    result: dict[str, Any] = {}
    for currency in currencies:
        known = Decimal(0)
        estimated = Decimal(0)
        reported_seen = covered = 0
        versions = set()
        pricing = set()
        pricing_complete = True
        for call in calls:
            cost = costs.get((call["run_id"], call["attempt_id"]))
            item = cost["estimates"].get(currency) if cost else None
            if item is None:
                continue
            assert cost is not None
            if item.get("known_amount") is not None:
                amount = _amount(item["known_amount"])
                with localcontext() as context:
                    context.prec = 50
                    if cost["usage_source"] == "estimated":
                        estimated += amount
                    elif cost["usage_source"] == "reported":
                        known += amount
                        reported_seen += 1
            for line in item.get("lines", []):
                versions.add(line["price_version"])
                rate, category = line.get("price_per_million"), line.get("category")
                if (
                    not isinstance(rate, str)
                    or not isinstance(category, str)
                    or not isinstance(call.get("provider"), str)
                    or not isinstance(call.get("model"), str)
                ):
                    pricing_complete = False
                else:
                    _amount(rate)
                    pricing.add(
                        json.dumps(
                            {
                                "provider": call["provider"],
                                "model": call["model"],
                                "category": category,
                                "currency": currency,
                                "price_version": line["price_version"],
                                "price_per_million": rate,
                            },
                            sort_keys=True,
                        )
                    )
            if not item.get("lines"):
                pricing_complete = False
            if (
                item.get("complete") is True
                and cost["usage_source"] == "reported"
                and call["usage_complete"]
                and item.get("known_amount") is not None
            ):
                covered += 1
        complete = not missing_cases and covered == len(calls)
        result[currency] = {
            "known_amount": str(known) if reported_seen else None,
            "estimated_usage_amount": str(estimated)
            if any(
                c["usage_source"] == "estimated" and currency in c["estimates"]
                for c in costs.values()
            )
            else None,
            "complete": complete,
            "covered_attempts": covered,
            "unknown_attempts": len(calls) - covered,
            "price_versions": sorted(versions),
            "pricing_fingerprint": _digest(sorted(pricing))
            if pricing_complete and pricing
            else None,
            "all_attempts_cost_per_success": str(known / successes)
            if complete and successes
            else None,
        }
    return {
        "by_currency": result,
        "unpriced_attempts": sum(
            not costs.get((c["run_id"], c["attempt_id"]), {}).get(
                "estimates",
            )
            for c in calls
        ),
        "missing_evidence_cases": missing_cases,
        "complete": bool(result) and all(c["complete"] for c in result.values()),
    }


def _cache(calls: list[dict[str, Any]]) -> dict[str, Any]:
    covered = [
        c
        for c in calls
        if c["usage_source"] == "reported"
        and "input_tokens" in c["usage"]
        and "cache_read_input_tokens" in c["usage"]
    ]
    eligible = [c for c in calls if c["usage_basis"] == "input_output"]
    covered = [c for c in covered if c["usage_basis"] == "input_output"]
    for call in covered:
        if call["usage"]["cache_read_input_tokens"] > call["usage"]["input_tokens"]:
            raise ValueError("Cache detail exceeds its input total")
    input_tokens = sum(c["usage"]["input_tokens"] for c in covered)
    cached = sum(c["usage"]["cache_read_input_tokens"] for c in covered)
    return {
        "scope": "reported input_output attempts with both input and cache-read counters",
        "eligible_attempts": len(eligible),
        "covered_attempts": len(covered),
        "coverage_ratio": _ratio(len(covered), len(eligible)),
        "covered_input_tokens": input_tokens if covered else None,
        "cache_read_tokens": cached if covered else None,
        "hit_ratio": cached / input_tokens if input_tokens else None,
    }


def _read_reports(
    reports: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
    int,
]:
    if not reports:
        raise ValueError("At least one evaluation report is required")
    cases: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    costs: dict[tuple[str, str], dict[str, Any]] = {}
    identities: set[tuple[str, str]] = set()
    missing = 0
    for report in reports:
        if report.get("schema_version") != 2 or type(report.get("schema_version")) is not int:
            raise ValueError("Only evaluation schema_version 2 is supported")
        results = report.get("results")
        if not isinstance(results, list):
            raise ValueError("Evaluation results must be a list")
        if _count(report.get("attempted"), "attempted") != len(results):
            raise ValueError("Attempted case count differs from results")
        if _count(report.get("total"), "total") < len(results):
            raise ValueError("Total case count is smaller than results")
        if _count(report.get("passed"), "passed") != sum(
            item.get("passed") is True for item in results
        ):
            raise ValueError("Passed case count differs from results")
        for case in results:
            case = _object(case, "case")
            if not isinstance(case.get("id"), str) or type(case.get("passed")) is not bool:
                raise ValueError("Case identity and boolean pass result are required")
            cases.append(case)
            evidence = _object(case.get("evidence", {}), "case evidence")
            if "usage" not in evidence:
                missing += 1
                continue
            ledger = _object(evidence["usage"], "usage ledger")
            rows = ledger.get("calls")
            if not isinstance(rows, list) or ledger.get("scope") != "exclusive":
                raise ValueError("Metrics require exclusive attempt evidence")
            if _count(ledger.get("attempt_count"), "attempt_count") != len(rows):
                raise ValueError("Attempt count differs from call evidence")
            by_id: dict[str, tuple[str, str]] = {}
            for row in rows:
                row = _object(row, "attempt")
                run_id, attempt_id = row.get("run_id"), row.get("attempt_id")
                if (
                    not isinstance(run_id, str)
                    or not run_id
                    or not isinstance(attempt_id, str)
                    or not attempt_id
                ):
                    raise ValueError("Attempt identity is missing")
                key = (run_id, attempt_id)
                if key in identities or key[1] in by_id:
                    raise ValueError("Duplicate attempt evidence; do not count report replays")
                identities.add(key)
                by_id[key[1]] = key
                snapshot = usage_snapshot(
                    row.get("usage"),
                    row.get("usage_source"),
                    basis=row.get("usage_basis", "input_output"),
                )
                if (
                    snapshot["usage"] != row.get("usage")
                    or snapshot["source"] != row.get("usage_source")
                    or snapshot["complete"] != row.get("usage_complete")
                ):
                    raise ValueError("Invalid attempt usage snapshot")
                calls.append({**row, "usage_basis": row.get("usage_basis", "input_output")})
            latest = evidence.get("costs", {}).get("latest", [])
            for cost in latest:
                cost_key = by_id.get(cost.get("attempt_id"))
                if cost_key is None or cost_key in costs:
                    raise ValueError("Cost snapshot has no unique matching attempt")
                if cost.get("usage_source") not in {"reported", "estimated", "unknown"}:
                    raise ValueError("Invalid cost usage source")
                _object(cost.get("estimates"), "cost estimates")
                costs[cost_key] = cost
    return cases, calls, costs, missing


def summarize_experiment(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Include every case and unique attempt, including failures and incomplete batches."""
    cases, calls, costs, missing = _read_reports(reports)
    successes = sum(c["passed"] for c in cases)
    failures = sum(c.get("status") not in {"completed"} for c in calls)
    retries = sum(type(c.get("attempt")) is int and c["attempt"] > 1 for c in calls)
    truncated = sum(c.get("stop_reason") in _TRUNCATED for c in calls)
    unknown = sum(c["usage_source"] == "unknown" for c in calls)
    retry_covered = sum(type(c.get("attempt")) is int and c["attempt"] >= 1 for c in calls)
    total = sum(report["total"] for report in reports)
    durations = [v for c in cases if (v := _number(c.get("duration_ms"))) is not None]
    request_durations = [v for c in calls if (v := _number(c.get("duration_ms"))) is not None]
    by_kind = {}
    for kind in sorted({str(c.get("kind", "unknown")) for c in calls}):
        selected = [c for c in calls if str(c.get("kind", "unknown")) == kind]
        by_kind[kind] = {
            "attempts": len(selected),
            "tokens": _tokens(selected),
            "costs": _costs(selected, costs, missing_cases=0, successes=successes),
            "latency": _latency(
                [v for c in selected if (v := _number(c.get("duration_ms"))) is not None],
                len(selected),
            ),
        }
    return {
        "schema_version": 1,
        "source": "evaluation_report_v2_exclusive_attempt_evidence",
        "reports": len(reports),
        "measurement_kinds": sorted({str(r.get("measurement_kind", "unknown")) for r in reports}),
        "tasks": {
            "successful": successes,
            "attempted": len(cases),
            "total": total,
            "failed": len(cases) - successes,
            "unattempted": total - len(cases),
            "success_ratio": _ratio(successes, total),
        },
        "attempts": {
            "total": len(calls),
            "failed_or_unfinished": failures,
            "failure_ratio": _ratio(failures, len(calls)),
            "truncated": truncated,
            "truncation_ratio": _ratio(truncated, len(calls)),
            "retries": retries,
            "retry_ratio": _ratio(retries, len(calls)) if retry_covered == len(calls) else None,
            "retry_metadata_covered": retry_covered,
            "unknown_usage": unknown,
            "unknown_usage_ratio": _ratio(unknown, len(calls)),
            "unknown_status": sum(c.get("status") == "unknown" for c in calls),
            "stop_reason_covered": sum(c.get("stop_reason") is not None for c in calls),
        },
        "tokens": _tokens(calls, missing_cases=missing),
        "cache": _cache(calls),
        "costs": _costs(calls, costs, missing_cases=missing, successes=successes),
        "task_latency": _latency(durations, len(cases)),
        "request_latency": _latency(request_durations, len(calls)),
        "by_kind": by_kind,
    }


def _request_profiles(evidence: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
    """Compare actual wire intent as well as the root Run's frozen generation."""
    calls = {(c["run_id"], c["attempt_id"]) for c in evidence.get("usage", {}).get("calls", [])}
    profiles: dict[str, set[str]] = {}
    observed = set()
    for attempt in evidence.get("model_attempts", []):
        key = (attempt.get("run_id"), attempt.get("attempt_id"))
        if key not in calls or key in observed:
            raise ValueError("Actual request profile has no unique matching attempt")
        observed.add(key)
        if any(attempt.get(field) != configuration[field] for field in ("provider", "model")):
            raise ValueError("Actual request provider/model differs from frozen configuration")
        generation = _object(attempt.get("generation"), "actual request generation")
        GenerationPatch.model_validate(generation)
        maximum = attempt.get("max_output_tokens")
        if type(maximum) is not int or maximum < 1:
            raise ValueError("Actual request output limit is missing")
        profile = json.dumps(
            {"generation": generation, "max_output_tokens": maximum},
            sort_keys=True,
            allow_nan=False,
        )
        profiles.setdefault(str(attempt.get("kind", "unknown")), set()).add(profile)
    if observed != calls:
        raise ValueError("Comparison requires every actual request generation profile")
    return {
        kind: [json.loads(value) for value in sorted(values)]
        for kind, values in sorted(profiles.items())
    }


def experiment_identity(report: Mapping[str, Any]) -> dict[str, Any]:
    """Stable comparison identity; execution IDs, paths and observed outcomes are excluded."""
    _read_reports([report])
    provenance = _object(report.get("provenance"), "provenance")
    runtime = _object(provenance.get("runtime"), "runtime provenance")
    if (
        not provenance.get("suite_source_sha256")
        or not provenance.get("inputs", {}).get("sha256")
        or not runtime.get("code_sha256")
    ):
        raise ValueError("Comparison requires suite, input and runtime fingerprints")
    cases: dict[str, Any] = {}
    for case in report["results"]:
        benchmark = _object(case.get("benchmark"), "frozen benchmark")
        if not all(
            benchmark.get(field)
            for field in (
                "suite_id",
                "suite_version",
                "suite_sha256",
                "task_version",
                "task_sha256",
                "split",
            )
        ):
            raise ValueError("Comparison requires frozen task identities")
        evidence = case["evidence"]
        main = next(
            (
                run
                for run in evidence.get("runs", [])
                if run.get("run_id") == evidence.get("main_run_id")
            ),
            None,
        )
        if (
            not main
            or not isinstance(main.get("generation"), dict)
            or not isinstance(main.get("context_fingerprints"), dict)
            or not isinstance(main.get("skill_snapshot"), dict)
        ):
            raise ValueError("Comparison requires actual generation, tool and Skill fingerprints")
        fingerprints = main["context_fingerprints"]
        skills = main["skill_snapshot"]
        if not fingerprints.get("tool_catalog_hash") or not skills.get("catalog_hash"):
            raise ValueError("Comparison requires tool and Skill fingerprints")
        GenerationPatch.model_validate(main["generation"])
        configuration = _object(case.get("configuration"), "configuration")
        if any(
            not isinstance(configuration.get(field), str) or not configuration[field]
            for field in ("provider", "model", "adapter")
        ):
            raise ValueError("Comparison requires actual provider and model configuration")
        if case["id"] in cases:
            raise ValueError("Duplicate task identity in one report")
        cases[case["id"]] = {
            "task": benchmark,
            "configuration": configuration,
            "generation": main["generation"],
            "context": fingerprints,
            "skills": {
                "catalog_hash": skills["catalog_hash"],
                "selected": skills.get("selected", []),
            },
            # Delivery UUIDs locate evidence, not a reproducible execution setting.
            # The original ID remains in the immutable case report and Event Log.
            "execution_config": {
                key: value for key, value in main.get("execution_config", {}).items()
                if key != "writer_delivery_id"
            },
            "requests": _request_profiles(evidence, configuration),
        }
    return {
        "measurement_kind": report.get("measurement_kind"),
        "mode": report.get("mode"),
        # Documentation/frontend-only commits and dirty flags do not change the
        # executed Python implementation. Their full provenance stays in reports.
        "runtime": {"code_sha256": runtime["code_sha256"], "python": runtime.get("python")},
        "suite_source_sha256": provenance.get("suite_source_sha256"),
        "inputs_sha256": provenance.get("inputs", {}).get("sha256"),
        "split": provenance.get("split"),
        "case_timeout": provenance.get("case_timeout"),
        "cases": cases,
    }


def _differences(left: Any, right: Any, path: str = "") -> dict[str, tuple[Any, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        result = {}
        for key in sorted(left.keys() | right.keys()):
            token = key.replace("~", "~0").replace("/", "~1")
            result.update(
                _differences(left.get(key, _MISSING), right.get(key, _MISSING), path + "/" + token)
            )
        return result
    return {} if _digest(left) == _digest(right) else {path: (left, right)}


@dataclass(frozen=True)
class ExperimentVariant:
    """Persist before the first candidate dispatch, outside this read-only API.

    Exact before/after values prevent a broad wildcard from hiding unrelated changes.
    recorded_at is Unix seconds, checked against all candidate request start times.
    An existing development baseline may inform this declaration; holdout must not.
    """

    name: str
    recorded_at: float
    differences: Mapping[str, tuple[Any, Any]]


def compare_experiments(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    *,
    variant: ExperimentVariant | None = None,
) -> dict[str, Any]:
    """Reject incomparable experiments; disclose latency even when a saving qualifies."""
    if not baseline or len(baseline) != len(candidate):
        raise ValueError("Comparison requires equal nonzero repetition counts")
    # The two arms must contain disjoint real attempts, even if snapshots differ.
    _read_reports([*baseline, *candidate])
    left = [experiment_identity(r) for r in baseline]
    right = [experiment_identity(r) for r in candidate]
    if any(identity != left[0] for identity in left) or any(
        identity != right[0] for identity in right
    ):
        raise ValueError("Repetitions must use one frozen identity per variant")
    changes = _differences(left[0], right[0])
    for case_id, base_case in left[0]["cases"].items():
        next_case = right[0]["cases"].get(case_id, {})
        previous_requests = base_case["requests"]
        next_requests = next_case.get("requests", {})
        if any(
            previous_requests[kind] != next_requests[kind]
            for kind in previous_requests.keys() & next_requests.keys()
        ):
            raise ValueError("Actual request generation must be frozen per purpose")
    if changes:
        if variant is None or not variant.name or changes != dict(variant.differences):
            raise ValueError("Undeclared experiment identity differences")
        if _number(variant.recorded_at) is None:
            raise ValueError("Variant recording time is invalid")
        for path in changes:
            if (
                path
                in {
                    "/mode",
                    "/measurement_kind",
                    "/suite_source_sha256",
                    "/inputs_sha256",
                    "/split",
                    "/case_timeout",
                }
                or "/task/" in path
                or "/generation/" in path
                or path.endswith(
                    (
                        "/configuration/provider",
                        "/configuration/model",
                        "/configuration/adapter",
                        "/configuration/endpoint_sha256",
                        "/configuration/max_output_tokens",
                    )
                )
            ):
                raise ValueError("Frozen tasks, model and generation cannot be variant differences")
        _, candidate_calls, _, _ = _read_reports(candidate)
        if (
            not candidate_calls
            or any(_number(c.get("started_at")) is None for c in candidate_calls)
            or variant.recorded_at > min(c["started_at"] for c in candidate_calls)
        ):
            raise ValueError("Variant must be recorded before all candidate model requests")
    elif variant is not None and variant.differences:
        raise ValueError("Declared variant differences do not match observed identities")
    before, after = summarize_experiment(baseline), summarize_experiment(candidate)
    base_passes = Counter(c["id"] for report in baseline for c in report["results"] if c["passed"])
    next_passes = Counter(c["id"] for report in candidate for c in report["results"] if c["passed"])
    regressions = sorted(name for name, count in base_passes.items() if next_passes[name] < count)
    completed = all(
        r.get("status") == "completed" and r["attempted"] == r["total"]
        for r in [*baseline, *candidate]
    )
    real = all(
        r.get("mode") == "live"
        and r.get("measurement_kind") == "provider_experiment"
        and r.get("billing_applicable") is True
        for r in [*baseline, *candidate]
    )
    quality = not regressions and after["tasks"]["successful"] >= before["tasks"]["successful"]
    token_saving = (
        before["tokens"]["complete"]
        and after["tokens"]["complete"]
        and after["tokens"]["total_tokens"] < before["tokens"]["total_tokens"]
    )
    currencies = {}
    for currency in sorted(
        before["costs"]["by_currency"].keys() | after["costs"]["by_currency"].keys()
    ):
        b = before["costs"]["by_currency"].get(currency)
        a = after["costs"]["by_currency"].get(currency)
        comparable = bool(
            b
            and a
            and b["complete"]
            and a["complete"]
            and b["price_versions"] == a["price_versions"]
            and b["pricing_fingerprint"] is not None
            and b["pricing_fingerprint"] == a["pricing_fingerprint"]
        )
        currencies[currency] = {
            "comparable": comparable,
            "decreased": bool(
                comparable and _amount(a["known_amount"]) < _amount(b["known_amount"])
            ),
        }
    fee_saving = any(c["decreased"] for c in currencies.values())
    return {
        "schema_version": 1,
        "baseline_identity_sha256": _digest(left[0]),
        "candidate_identity_sha256": _digest(right[0]),
        "variant": {
            "name": variant.name,
            "recorded_at": variant.recorded_at,
            "differences": dict(variant.differences),
        }
        if variant
        else None,
        "baseline": before,
        "candidate": after,
        "quality_preserved": quality,
        "regressed_tasks": regressions,
        "complete_batches": completed,
        "provider_experiments": real,
        "reported_tokens_decreased": token_saving,
        "cost_comparison": currencies,
        "improvement_demonstrated": bool(
            real and completed and quality and (token_saving or fee_saving)
        ),
        "latency_change_ms": {
            name: {
                key: after[name][key] - before[name][key]
                if after[name][key] is not None and before[name][key] is not None
                else None
                for key in ("mean_ms", "p50_ms", "p95_ms")
            }
            for name in ("task_latency", "request_latency")
        },
    }
