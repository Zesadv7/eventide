"""Usage normalization and attempt accounting preserve uncertainty and exact ownership."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from eventide.normalization import redact
from eventide.telemetry import build_run_trace
from eventide.usage import build_usage_ledger, normalize_provider_usage, usage_snapshot


def event(kind, seq, payload=None, *, run="run", session="session"):
    return {
        "event_id": f"{session}:{seq}", "workspace_id": "workspace",
        "session_id": session, "run_id": run, "session_seq": seq,
        "type": kind, "ts": float(seq), "payload": payload or {}, "partial": False,
    }


def attempt(seq=1, *, attempt_id="try1", run="run", session="session", **changes):
    payload = {"request_id": "request", "attempt_id": attempt_id, "attempt": 1,
               "kind": "agent", "status": "completed", "usage_source": "reported",
               "usage": {"input_tokens": 10, "output_tokens": 2}, **changes}
    return event("model.attempt_finished", seq, payload, run=run, session=session)


@pytest.mark.parametrize("usage", [None, {}, SimpleNamespace(), {"input_tokens": None}])
def test_missing_usage_is_unknown_not_reported_zero(usage):
    assert normalize_provider_usage("openai_responses", usage) == {}
    assert usage_snapshot(usage) == {"usage": {}, "source": "unknown", "complete": False}


def test_reported_zero_is_real_and_invalid_values_are_not_coerced():
    result = normalize_provider_usage("openai_responses", {
        "input_tokens": 0, "output_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    })
    assert result == {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "reasoning_tokens": 0}
    assert usage_snapshot(result)["complete"] is True
    for invalid in (True, -1, "12", 12.0, float("nan")):
        assert normalize_provider_usage("openai_responses", {
            "input_tokens": invalid, "output_tokens": None,
        }) == {}


def test_openai_and_deepseek_details_are_subsets_not_extra_totals():
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
                            prompt_cache_miss_tokens=20)
    result = normalize_provider_usage("openai_compatible", usage)
    assert result == {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 80, "cache_miss_input_tokens": 20,
                      "reasoning_tokens": 12}
    deepseek = normalize_provider_usage("openai_compatible", {
        "prompt_tokens": 100, "completion_tokens": 2,
        "prompt_cache_hit_tokens": 90, "prompt_cache_miss_tokens": 10,
    })
    assert deepseek["input_tokens"] == 100
    assert deepseek["cache_read_input_tokens"] == 90
    assert normalize_provider_usage("openai_compatible", {
        "prompt_cache_hit_tokens": 90,
    }) == {"cache_read_input_tokens": 90}


def test_anthropic_input_total_includes_its_disjoint_cache_buckets_once():
    result = normalize_provider_usage("anthropic", SimpleNamespace(
        input_tokens=10, output_tokens=3,
        cache_read_input_tokens=50, cache_creation_input_tokens=20,
    ))
    assert result == {"input_tokens": 80, "output_tokens": 3, "uncached_input_tokens": 10,
                      "cache_read_input_tokens": 50, "cache_creation_input_tokens": 20}
    assert normalize_provider_usage("anthropic", {"output_tokens": 1,
        "cache_read_input_tokens": 8}) == {"output_tokens": 1, "cache_read_input_tokens": 8}


def test_estimates_remain_separate_and_incomplete():
    snapshot = usage_snapshot({"input_tokens": 5, "output_tokens": 1}, "estimated")
    assert snapshot["complete"] is False
    ledger = build_usage_ledger([attempt(usage_source="estimated")])
    assert ledger["reported_totals"] == {}
    assert ledger["estimated_totals"] == {"input_tokens": 10, "output_tokens": 2}
    assert ledger["complete"] is False


def test_ledger_deduplicates_replays_and_ignores_inclusive_summaries():
    finish = attempt()
    events = [finish, deepcopy(finish),
              event("model.response", 2, {"usage": {"input_tokens": 999}}),
              event("worker.completed", 3, {"usage": {"input_tokens": 999}}),
              event("run.completed", 4, {"usage": {"input_tokens": 999}}),
              attempt(1, attempt_id="try1", run="child", session="child-session")]
    before = deepcopy(events)
    ledger = build_usage_ledger(events)
    assert ledger == build_usage_ledger(reversed(events))
    assert events == before
    assert ledger["attempt_count"] == 2
    assert ledger["reported_totals"] == {"input_tokens": 20, "output_tokens": 4}
    assert ledger["scope"] == "exclusive"


def test_retries_and_unfinished_requests_remain_unknown_after_replay():
    events = [event("model.attempt_started", 1, {
        "request_id": "request", "attempt_id": "try1", "attempt": 1}),
        attempt(2, usage=None, usage_source="unknown", status="failed"),
        event("model.retry", 3),
        attempt(4, attempt_id="try2", attempt=2, kind="context_summary"),
        event("context.summary_generated", 5, {"usage": {"input_tokens": 10}}),
        event("context.compacted", 6, {"usage": {"input_tokens": 10}}),
        event("model.attempt_started", 7, {"request_id": "next", "attempt_id": "try3"})]
    ledger = build_usage_ledger(events)
    assert ledger["attempt_count"] == 3
    assert ledger["unknown_attempts"] == ledger["incomplete_attempts"] == 2
    assert ledger["reported_totals"] == {"input_tokens": 10, "output_tokens": 2}
    assert ledger["calls"][-1]["status"] == "unknown"
    assert ledger["calls"][-1]["finished_at"] is None
    assert ledger["complete"] is False


def test_legacy_fallback_does_not_double_count_summary_or_terminal():
    events = [event("model.response", 1, {"usage": {"input_tokens": 2}}),
              event("context.summary_generated", 2, {"usage": {"input_tokens": 3}}),
              event("context.compacted", 3, {"usage": {"input_tokens": 3}}),
              event("worker.completed", 4, {"usage": {"input_tokens": 100}}),
              event("run.completed", 5, {"usage": {"input_tokens": 100}})]
    ledger = build_usage_ledger(events + [events[0]])
    assert ledger["reported_totals"] == {"input_tokens": 5}
    assert ledger["attempt_count"] == 2
    assert ledger["complete"] is False
    assert all(not call["request_observed"] for call in ledger["calls"])


def test_conflicting_attempts_reject_silent_bill_changes_and_partial_facts_skip():
    with pytest.raises(ValueError, match="Conflicting"):
        build_usage_ledger([attempt(), attempt(usage={"input_tokens": 90})])
    with pytest.raises(ValueError, match="identity"):
        build_usage_ledger([event("model.attempt_started", 1,
            {"attempt_id": "try1", "request_id": "other", "attempt": 1}), attempt()])
    with pytest.raises(ValueError, match="attempt_id"):
        build_usage_ledger([event("model.attempt_finished", 1)])
    partial = attempt()
    partial["partial"] = True
    assert build_usage_ledger([partial])["calls"] == []


def test_only_numeric_public_token_fields_bypass_secret_redaction():
    assert redact({"max_output_tokens": 512, "max_tokens": 32, "reasoning_tokens": 0,
                   "cache_miss_input_tokens": 1}) == {
        "max_output_tokens": 512, "max_tokens": "[REDACTED]", "reasoning_tokens": 0,
        "cache_miss_input_tokens": 1}
    for invalid in ("private", True, -1, {"nested": "private"}):
        assert redact({"input_tokens": invalid}) == {"input_tokens": "[REDACTED]"}
    assert redact({"arbitrary_tokens": 7}) == {"arbitrary_tokens": "[REDACTED]"}


def test_trace_uses_attempts_not_duplicate_legacy_events_and_does_not_export_ids():
    secret = "DO_NOT_EXPORT_secret"
    events = [event("run.started", 1), event("model.request", 2),
              event("model.attempt_started", 3, {"attempt_id": secret,
                    "request_id": secret, "attempt": 1}),
              attempt(4, attempt_id=secret, request_id=secret),
              event("model.response", 5, {"usage": {"input_tokens": 999}}),
              event("run.completed", 6)]
    trace = build_run_trace(events)
    models = [span for span in trace["spans"] if span["name"] == "eventide.model"]
    assert len(models) == 1
    assert models[0]["attributes"]["eventide.usage.input_tokens"] == 10
    assert secret not in str(trace)
    no_usage = build_run_trace([event("run.started", 1), attempt(2, usage={})])
    run = next(span for span in no_usage["spans"] if span["name"] == "eventide.run")
    assert "eventide.usage.input_tokens" not in run["attributes"]
    assert run["attributes"]["eventide.usage.complete"] is False


def test_request_metadata_replays_without_deriving_latency_from_wall_clock():
    start = event("model.attempt_started", 10, {
        "attempt_id": "try1", "request_id": "request", "attempt": 1,
        "provider": "openai_compatible", "model": "model-a",
    })
    finish = attempt(5, duration_ms=0, stop_reason="length")
    ledger = build_usage_ledger([start, finish, deepcopy(finish)])
    row = ledger["calls"][0]
    assert ledger["attempt_count"] == 1
    assert row["provider"] == "openai_compatible"
    assert row["model"] == "model-a"
    assert row["stop_reason"] == "length"
    assert row["duration_ms"] == 0  # A reported zero is not missing; wall clock moved backwards.
    in_flight = build_usage_ledger([start])["calls"][0]
    assert in_flight["duration_ms"] is None
    assert in_flight["stop_reason"] is None
    assert in_flight["provider"] == "openai_compatible"
    assert in_flight["usage_source"] == "unknown"


@pytest.mark.parametrize("duration", [None, True, -1, "12", float("nan"), float("inf")])
def test_invalid_or_missing_latency_remains_unknown(duration):
    row = build_usage_ledger([attempt(duration_ms=duration)])["calls"][0]
    assert row["duration_ms"] is None


def test_failed_and_legacy_request_metadata_preserve_only_observed_values():
    failed = build_usage_ledger([attempt(
        status="failed", usage={}, duration_ms=12.5, provider="scripted",
    )])["calls"][0]
    assert failed["duration_ms"] == 12.5
    assert failed["usage_source"] == "unknown"
    assert failed["stop_reason"] is None
    legacy = build_usage_ledger([event("model.response", 99, {
        "provider": "anthropic", "stop_reason": "end_turn",
    })])["calls"][0]
    assert legacy["duration_ms"] is None  # No invented zero or timestamp subtraction.
    assert legacy["provider"] == "anthropic"
    assert legacy["stop_reason"] == "end_turn"
    assert legacy["usage_complete"] is False
