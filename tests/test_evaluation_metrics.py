"""All-attempt statistics and guarded, independently graded experiment comparisons."""

import copy
import json
from pathlib import Path

import pytest

import eventide.evaluation as evaluation
from eventide.evaluation_metrics import (
    ExperimentVariant,
    compare_experiments,
    experiment_identity,
    summarize_experiment,
)
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


def _call(number=1, *, run="run", usage=None, source="reported", kind="agent", **overrides):
    return {
        "run_id": run,
        "attempt_id": f"{run}-{number}",
        "attempt": number,
        "request_id": f"{run}-request",
        "kind": kind,
        "status": "completed",
        "provider": "openai_compatible",
        "model": "test-model",
        "usage": {"input_tokens": 100, "output_tokens": 20} if usage is None else usage,
        "usage_source": source,
        "usage_complete": source == "reported",
        "usage_basis": "input_output",
        "duration_ms": 10 * number,
        "started_at": 100 + number,
        "stop_reason": "end_turn",
        **overrides,
    }


def _cost(call, *, currency="CNY", amount="0.1", complete=True, version="price-v1"):
    return {
        "attempt_id": call["attempt_id"],
        "usage_source": call["usage_source"],
        "estimates": {
            currency: {
                "known_amount": amount,
                "complete": complete,
                "lines": [
                    {"price_version": version, "category": "input_tokens", "price_per_million": "1"}
                ],
            }
        },
    }


def _report(*, run="run", passed=True, calls=None, costs=None, mode="live"):
    calls = [_call(run=run)] if calls is None else calls
    costs = [_cost(c) for c in calls] if costs is None else costs
    return {
        "schema_version": 2,
        "status": "completed",
        "mode": mode,
        "measurement_kind": "provider_experiment"
        if mode == "live"
        else "scripted_mechanism_validation",
        "billing_applicable": mode == "live",
        "total": 1,
        "attempted": 1,
        "passed": int(passed),
        "provenance": {
            "runtime": {"commit": "abc", "dirty": False, "code_sha256": "source", "python": "3.12"},
            "suite_source_sha256": "suite",
            "inputs": {"sha256": "inputs"},
            "split": "development",
            "case_timeout": 300,
        },
        "results": [
            {
                "id": "fix-it",
                "passed": passed,
                "duration_ms": 100,
                "status": "completed" if passed else "interrupted",
                "configuration": {
                    "provider": "openai_compatible",
                    "model": "test-model",
                    "adapter": "configured_provider",
                    "context_limit": 5000,
                },
                "benchmark": {
                    "suite_id": "frozen",
                    "suite_version": 1,
                    "suite_sha256": "suite",
                    "task_version": 1,
                    "task_sha256": "task",
                    "split": "development",
                },
                "evidence": {
                    "main_run_id": run,
                    "runs": [
                        {
                            "run_id": run,
                            "generation": {"max_output_tokens": 512},
                            "context_fingerprints": {
                                "instruction_hash": "i",
                                "tool_catalog_hash": "t",
                            },
                            "skill_snapshot": {"catalog_hash": "s", "selected": []},
                        }
                    ],
                    "usage": {"scope": "exclusive", "calls": calls, "attempt_count": len(calls)},
                    "costs": {"latest": costs},
                    "model_attempts": [
                        {
                            **{
                                key: call[key]
                                for key in (
                                    "run_id",
                                    "attempt_id",
                                    "provider",
                                    "model",
                                    "kind",
                                )
                            },
                            "generation": {"max_output_tokens": 512},
                            "max_output_tokens": 512,
                        }
                        for call in calls
                    ],
                },
            }
        ],
    }


def test_all_attempts_include_failure_retry_and_kind_overhead_without_detail_double_count():
    calls = [
        _call(
            1,
            usage={},
            source="unknown",
            usage_complete=False,
            status="failed",
            duration_ms=None,
            stop_reason=None,
        ),
        _call(
            2,
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 50,
                "reasoning_tokens": 15,
            },
            stop_reason="length",
        ),
        _call(1, run="summary", kind="context_summary"),
        _call(1, run="child", kind="worker"),
        _call(1, run="rewrite", kind="query_rewrite"),
        _call(
            1,
            run="embedding",
            kind="embedding",
            usage={"input_tokens": 10},
            usage_basis="input_only",
        ),
    ]
    report = _report(calls=calls, costs=[_cost(c, complete=c["usage_complete"]) for c in calls])
    # A stale terminal/parent total must never contribute to this projection.
    report["usage"] = {"reported_totals": {"input_tokens": 999999999}}
    snapshot = copy.deepcopy(report)
    result = summarize_experiment([report])
    assert report == snapshot  # API is read-only.
    assert result["tokens"]["reported"]["input_tokens"] == 410
    assert result["tokens"]["reported"]["output_tokens"] == 80
    assert result["tokens"]["total_tokens"] is None
    assert result["attempts"]["total"] == 6
    assert result["attempts"]["failed_or_unfinished"] == 1
    assert result["attempts"]["truncated"] == result["attempts"]["retries"] == 1
    assert result["attempts"]["unknown_usage_ratio"] == 1 / 6
    assert result["request_latency"]["observed"] == 5
    assert result["request_latency"]["p50_ms"] == 10
    assert result["request_latency"]["p95_ms"] == 20
    assert result["cache"]["coverage_ratio"] == 0.2
    assert result["cache"]["hit_ratio"] == 0.5
    assert set(result["by_kind"]) == {
        "agent",
        "context_summary",
        "worker",
        "query_rewrite",
        "embedding",
    }
    assert result["costs"]["by_currency"]["CNY"]["all_attempts_cost_per_success"] is None


def test_reported_totals_count_only_input_output_and_keep_zero_cache_as_observed():
    result = summarize_experiment(
        [
            _report(
                calls=[
                    _call(
                        usage={
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 20,
                            "reasoning_tokens": 15,
                        }
                    )
                ]
            )
        ]
    )
    assert result["tokens"]["total_tokens"] == 120
    assert result["cache"]["hit_ratio"] == 0
    assert result["cache"]["covered_attempts"] == 1
    assert result["costs"]["by_currency"]["CNY"]["all_attempts_cost_per_success"] == "0.1"


def test_missing_usage_prices_estimates_and_unattempted_cases_never_become_free_zero():
    first = _report(calls=[_call(source="estimated", usage_complete=False)])
    first["total"] = 2
    first["status"] = "aborted"
    second = _report(run="missing", passed=False)
    second["results"][0]["evidence"] = {}
    result = summarize_experiment([first, second])
    assert result["tasks"] == {
        "successful": 1,
        "attempted": 2,
        "total": 3,
        "failed": 1,
        "unattempted": 1,
        "success_ratio": 1 / 3,
    }
    assert result["tokens"]["reported"] == {}
    assert result["tokens"]["estimated"] == {"input_tokens": 100, "output_tokens": 20}
    assert result["tokens"]["missing_evidence_cases"] == 1
    assert result["tokens"]["total_tokens"] is None
    assert not result["costs"]["complete"]
    assert result["costs"]["by_currency"]["CNY"]["known_amount"] is None
    no_prices = summarize_experiment([_report(costs=[])])
    assert no_prices["costs"]["by_currency"] == {}
    assert no_prices["costs"]["unpriced_attempts"] == 1
    assert not no_prices["costs"]["complete"]


def test_currencies_are_separate_and_no_success_has_no_cost_per_success():
    one = _call()
    cost = _cost(one)
    cost["estimates"]["USD"] = _cost(one, currency="USD", amount="0.02")["estimates"]["USD"]
    result = summarize_experiment([_report(passed=False, costs=[cost])])
    assert result["costs"]["by_currency"]["CNY"]["known_amount"] == "0.1"
    assert result["costs"]["by_currency"]["USD"]["known_amount"] == "0.02"
    assert all(
        c["all_attempts_cost_per_success"] is None for c in result["costs"]["by_currency"].values()
    )


@pytest.mark.parametrize("damage", ["replayed", "conflicting", "count", "invalid_usage", "cost"])
def test_duplicate_or_invalid_evidence_cannot_inflate_statistics(damage):
    report = _report()
    if damage in {"replayed", "conflicting"}:
        second = copy.deepcopy(report)
        if damage == "conflicting":
            second["results"][0]["evidence"]["usage"]["calls"][0]["usage"]["input_tokens"] = 999
        reports = [report, second]
    else:
        reports = [report]
        evidence = report["results"][0]["evidence"]
        if damage == "count":
            evidence["usage"]["attempt_count"] = 3
        elif damage == "invalid_usage":
            evidence["usage"]["calls"][0]["usage"]["input_tokens"] = True
        else:
            evidence["costs"]["latest"][0]["attempt_id"] = "unrelated"
    with pytest.raises(ValueError):
        summarize_experiment(reports)


def test_quality_and_all_attempt_savings_are_required_and_latency_is_disclosed():
    baseline = _report()
    candidate = _report(
        run="candidate",
        calls=[
            _call(
                run="candidate",
                usage={
                    "input_tokens": 80,
                    "output_tokens": 20,
                },
            )
        ],
    )
    candidate["results"][0]["duration_ms"] = 150
    result = compare_experiments([baseline], [candidate])
    assert result["improvement_demonstrated"] and result["reported_tokens_decreased"]
    assert result["latency_change_ms"]["task_latency"]["p95_ms"] == 50
    candidate["results"][0]["passed"] = False
    candidate["passed"] = 0
    result = compare_experiments([baseline], [candidate])
    assert not result["improvement_demonstrated"]
    assert result["regressed_tasks"] == ["fix-it"]


def test_offline_incomplete_or_unknown_attempts_cannot_claim_improvement():
    baseline = _report(mode="offline")
    candidate = _report(
        run="candidate",
        mode="offline",
        calls=[
            _call(
                run="candidate",
                usage={
                    "input_tokens": 1,
                    "output_tokens": 1,
                },
            )
        ],
    )
    assert not compare_experiments([baseline], [candidate])["improvement_demonstrated"]
    baseline = _report()
    candidate = _report(
        run="candidate",
        calls=[_call(run="candidate", usage={}, source="unknown", usage_complete=False)],
        costs=[],
    )
    assert not compare_experiments([baseline], [candidate])["improvement_demonstrated"]
    candidate = _report(run="candidate")
    candidate["status"] = "aborted"
    assert not compare_experiments([baseline], [candidate])["improvement_demonstrated"]


def test_fee_savings_require_same_currency_complete_usage_and_fixed_price_versions():
    baseline = _report()
    candidate = _report(run="candidate", costs=[_cost(_call(run="candidate"), amount="0.05")])
    result = compare_experiments([baseline], [candidate])
    assert result["improvement_demonstrated"]
    assert result["cost_comparison"]["CNY"] == {"comparable": True, "decreased": True}
    candidate["results"][0]["evidence"]["costs"]["latest"][0]["estimates"]["CNY"]["lines"][0][
        "price_version"
    ] = "new-cheaper-price"
    assert not compare_experiments([baseline], [candidate])["improvement_demonstrated"]


def test_non_runtime_commit_changes_do_not_invalidate_same_executed_code():
    baseline, candidate = _report(), _report(run="candidate")
    candidate["provenance"]["runtime"].update(commit="docs-only-new-commit", dirty=True)
    assert experiment_identity(baseline) == experiment_identity(candidate)
    assert compare_experiments([baseline], [candidate])["quality_preserved"]


@pytest.mark.parametrize("changed_rate", [True, False])
def test_same_price_name_cannot_hide_different_or_missing_rate_basis(changed_rate):
    baseline = _report()
    candidate = _report(run="candidate", costs=[_cost(_call(run="candidate"), amount="0.01")])
    line = candidate["results"][0]["evidence"]["costs"]["latest"][0]["estimates"]["CNY"]["lines"][0]
    if changed_rate:
        line["price_per_million"] = "0.1"
    else:
        del line["price_per_million"]
    result = compare_experiments([baseline], [candidate])
    assert not result["improvement_demonstrated"]
    assert not result["cost_comparison"]["CNY"]["comparable"]


def test_actual_helper_request_generation_cannot_change_behind_unchanged_run_config():
    baseline, candidate = _report(), _report(run="candidate")
    candidate["results"][0]["evidence"]["model_attempts"][0]["max_output_tokens"] = 8
    with pytest.raises(ValueError, match="Actual request generation"):
        compare_experiments([baseline], [candidate])


def test_absent_retry_metadata_and_cache_details_are_unknown():
    call = _call(attempt=None, duration_ms=None)
    report = _report(calls=[call])
    result = summarize_experiment([report])
    assert result["attempts"]["retry_ratio"] is None
    assert result["attempts"]["retry_metadata_covered"] == 0
    assert result["request_latency"]["p95_ms"] is None
    assert result["cache"]["hit_ratio"] is None
    assert result["cache"]["coverage_ratio"] == 0


@pytest.mark.parametrize(
    "damage", ["usage_source", "partial_zero", "cache_overflow", "cost_number"]
)
def test_ambiguous_measurements_cannot_be_made_precise_by_coercion(damage):
    report = _report()
    evidence = report["results"][0]["evidence"]
    if damage == "usage_source":
        evidence["usage"]["calls"][0]["usage_source"] = "unknown"
    elif damage == "partial_zero":
        evidence["usage"]["calls"][0]["usage"] = {"input_tokens": 0}
    elif damage == "cache_overflow":
        evidence["usage"]["calls"][0]["usage"]["cache_read_input_tokens"] = 101
    else:
        evidence["costs"]["latest"][0]["estimates"]["CNY"]["known_amount"] = "NaN"
    with pytest.raises(ValueError):
        summarize_experiment([report])


@pytest.mark.parametrize("field", ["model", "generation", "task", "inputs", "tool"])
def test_comparison_rejects_unfrozen_model_task_generation_or_catalog(field):
    baseline, candidate = _report(), _report(run="candidate")
    case = candidate["results"][0]
    if field == "model":
        case["configuration"]["model"] = "different"
    elif field == "generation":
        case["evidence"]["runs"][0]["generation"]["max_output_tokens"] = 32
    elif field == "task":
        case["benchmark"]["task_version"] = 2
    elif field == "inputs":
        candidate["provenance"]["inputs"]["sha256"] = "changed"
    else:
        case["evidence"]["runs"][0]["context_fingerprints"]["tool_catalog_hash"] = "changed"
    with pytest.raises(ValueError, match="Undeclared|Actual request provider/model"):
        compare_experiments([baseline], [candidate])


def test_variant_declaration_is_exact_early_and_cannot_exempt_frozen_generation():
    baseline, candidate = _report(), _report(run="candidate")
    candidate["results"][0]["configuration"]["context_limit"] = 4000
    declaration = {"/cases/fix-it/configuration/context_limit": (5000, 4000)}
    variant = ExperimentVariant("smaller-context", 100, declaration)
    result = compare_experiments([baseline], [candidate], variant=variant)
    assert result["variant"]["differences"] == declaration
    with pytest.raises(ValueError, match="before all"):
        compare_experiments(
            [baseline], [candidate], variant=ExperimentVariant("late", 102, declaration)
        )
    with pytest.raises(ValueError, match="Undeclared"):
        compare_experiments(
            [baseline],
            [candidate],
            variant=ExperimentVariant(
                "wrong",
                100,
                {"/cases/fix-it/configuration/context_limit": (5000, 3000)},
            ),
        )
    candidate = _report(run="candidate")
    candidate["results"][0]["evidence"]["runs"][0]["generation"]["max_output_tokens"] = 32
    with pytest.raises(ValueError, match="cannot be variant"):
        compare_experiments(
            [baseline],
            [candidate],
            variant=ExperimentVariant(
                "low-output",
                100,
                {
                    "/cases/fix-it/generation/max_output_tokens": (512, 32),
                },
            ),
        )


def test_development_baseline_may_precede_variant_but_candidate_may_not():
    baseline, candidate = _report(), _report(run="candidate")
    baseline["results"][0]["evidence"]["usage"]["calls"][0]["started_at"] = 10
    candidate["results"][0]["configuration"]["context_limit"] = 4000
    differences = {"/cases/fix-it/configuration/context_limit": (5000, 4000)}
    result = compare_experiments(
        [baseline],
        [candidate],
        variant=ExperimentVariant("from-development", 50, differences),
    )
    assert result["quality_preserved"]
    with pytest.raises(ValueError, match="before all candidate"):
        compare_experiments(
            [baseline],
            [candidate],
            variant=ExperimentVariant("after-candidate", 102, differences),
        )


def test_repetitions_must_match_and_pass_gains_cannot_hide_a_regressed_task():
    baseline, candidate = _report(), _report(run="candidate")
    with pytest.raises(ValueError, match="repetition"):
        compare_experiments([baseline, _report(run="repeat")], [candidate])
    other = _report(run="other", passed=False)["results"][0]
    other["id"] = "other-task"
    baseline["results"].append(other)
    baseline.update(total=2, attempted=2)
    candidate = copy.deepcopy(baseline)
    for index, case in enumerate(candidate["results"]):
        case["passed"] = not case["passed"]
        row = case["evidence"]["usage"]["calls"][0]
        row["run_id"] = f"candidate-{index}"
        case["evidence"]["model_attempts"][0]["run_id"] = row["run_id"]
    result = compare_experiments([baseline], [candidate])
    assert result["baseline"]["tasks"]["successful"] == result["candidate"]["tasks"]["successful"]
    assert not result["quality_preserved"] and result["regressed_tasks"] == ["fix-it"]


async def test_real_runtime_report_uses_exclusive_failed_retry_and_loaded_skill_evidence(
    isolated_workspace,
):
    suite = isolated_workspace / "cases.json"
    suite.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "real-evidence",
                        "prompt": "answer",
                        "script": [
                            {"error": "temporary", "retryable": True},
                            {"text": "done", "usage": {"input_tokens": 10, "output_tokens": 4}},
                        ],
                    }
                ]
            }
        )
    )
    report = await evaluation.run_evaluations(suite, settings=settings_for(isolated_workspace))
    result = summarize_experiment([report])
    assert result["tasks"]["successful"] == 1
    assert result["attempts"]["total"] == 2 and result["attempts"]["retries"] == 1
    assert result["tokens"]["reported"] == {"input_tokens": 10, "output_tokens": 4}
    assert result["tokens"]["total_tokens"] is None
    with pytest.raises(ValueError, match="frozen task"):
        experiment_identity(report)


@pytest.mark.parametrize("live", [False, True])
async def test_delivery_repair_provenance_tracks_actual_provider_without_changing_grading(
    isolated_workspace,
    monkeypatch,
    live,
):
    from eventide.evaluation import load_suite

    # A configured adapter delegates to deterministic responses for this network-free test.
    class ConfiguredProvider:
        def __init__(self, script):
            self.scripted = ScriptedProvider(script)

        async def complete(self, request):
            return await self.scripted.complete(request)

    case = next(
        c
        for c in load_suite(Path(__file__).resolve().parents[1] / "evals/benchmark.yaml")
        if c["benchmark"]["category"] == "recovery_delivery"
    )
    if live:
        monkeypatch.setattr(
            evaluation, "build_provider", lambda settings: ConfiguredProvider(case["script"])
        )
    suite = isolated_workspace / "delivery.json"
    # This exercises the existing grade contract, not a weakened synthetic grader.
    suite.write_text(json.dumps({"cases": [case]}))
    if live:
        # The untrusted stand-alone frozen-live guard remains in force. Exercise the
        # same private case runner using the configured fake adapter, without a network.
        directory = isolated_workspace / "artifacts"
        record, cancelled = await evaluation._evaluate_case(
            case,
            1,
            settings_for(isolated_workspace),
            directory,
            live=True,
            prices=(),
            case_timeout=300,
        )
        assert not cancelled and record.passed
        verification = record.verification
    else:
        report = await evaluation.run_evaluations(suite, settings=settings_for(isolated_workspace))
        assert report["passed"] == 1
        verification = report["results"][0]["verification"]
    assert verification["repair_by"] == (
        "configured_provider_tools" if live else "scripted_provider_tools"
    )
    assert verification["tests_unchanged"] and verification["recovered_after_restart"]
    assert verification["passed"]
