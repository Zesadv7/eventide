"""MK-08B model step failure classification and safe Host retry contracts.

Every retry decision must come from evidence: transport categories, honored
Retry-After, protocol and provider-side tool barriers, per-attempt ledger
facts, and previews that never mix two physical attempts.
"""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from eventide.host import RuntimeHost
from eventide.model_failures import (
    MODEL_STEP_ATTEMPTS,
    RETRY_BACKOFF_CAP_SECONDS,
    ModelFailure,
    classify_failure,
    classify_transport,
    parse_retry_after,
    retry_delay,
)
from eventide.models import ModelRequest, ModelResponse, RunRequest
from eventide.previews import PreviewStore
from eventide.providers import ProviderError, ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_runtime import settings_for


def request() -> ModelRequest:
    return ModelRequest("system", [{"role": "user", "content": "hi"}], [], "model", 32)


def retry_events(host: RuntimeHost, run_id: str) -> list[dict]:
    return [
        event["payload"]
        for event in host.store.run_events(run_id)
        if event["type"] == "model.retry"
    ]


def test_backoff_schedule_doubles_capped_with_jitter_bounds():
    for attempt, base in enumerate((1, 2, 4, 8, 16, 32, 32, 32, 32), 1):
        for _ in range(20):
            delay = retry_delay(attempt, base=1.0, jitter=lambda d: d)
            assert delay == base
    assert retry_delay(50, base=1.0) == RETRY_BACKOFF_CAP_SECONDS
    for _ in range(50):
        delay = retry_delay(1, base=1.0)
        assert 1.0 <= delay <= 1.5
    with pytest.raises(ValueError):
        retry_delay(0, base=1.0)


def test_retry_after_is_a_floor_above_the_schedule():
    assert retry_delay(1, base=1.0, retry_after=7.5, jitter=lambda d: d) == 7.5
    assert retry_delay(3, base=1.0, retry_after=2.0, jitter=lambda d: d) == 4.0


def test_retry_after_header_parsing():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("  3 ") == 3.0
    assert parse_retry_after(9) == 9.0
    assert parse_retry_after("0") is None
    assert parse_retry_after("-2") is None
    assert parse_retry_after("not-a-date") is None
    assert parse_retry_after("") is None
    future = parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=1_700_000_000.0)
    assert future is not None and future > 0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1_700_000_000.0) is None


def test_transport_classification_covers_all_contract_categories():
    def status(code: int, retry_after=None):
        headers = {} if retry_after is None else {"retry-after": str(retry_after)}
        return SimpleNamespace(
            status_code=code, response=SimpleNamespace(headers=headers),
        )

    assert classify_failure(asyncio.TimeoutError()) == ModelFailure("timeout", True)
    rate = classify_failure(status(429, retry_after=12))
    assert (rate.category, rate.retryable, rate.retry_after) == ("rate_limited", True, 12.0)
    assert classify_failure(status(408)).category == "timeout"
    for code in (500, 502, 503, 504, 529):
        assert classify_failure(status(code)).category == "temporary"
        assert classify_failure(status(code)).retryable
    for code in (401, 403):
        failure = classify_failure(status(code))
        assert failure.category == "auth" and not failure.retryable
    assert classify_failure(status(402)).category == "billing"
    for code in (400, 404, 409, 422):
        failure = classify_failure(status(code))
        assert failure.category == "rejected" and not failure.retryable
    text = classify_failure(RuntimeError("Error code: 401 - invalid credentials"))
    assert text.category == "auth" and not text.retryable
    assert classify_failure(RuntimeError("Insufficient Balance")).category == "billing"
    assert classify_failure(RuntimeError("Connection error.")).category == "network"
    assert classify_failure(RuntimeError("Request timed out.")).category == "timeout"
    assert classify_failure(RuntimeError("overloaded_error")).category == "temporary"
    unknown = classify_failure(ValueError("very strange failure"))
    assert unknown.category == "unknown" and not unknown.retryable


def test_legacy_retryable_flag_maps_to_network_but_cannot_authorize_auth():
    legacy = classify_failure(ProviderError("connection lost", retryable=True))
    assert legacy.category == "network" and legacy.retryable
    denied = classify_failure(
        ProviderError("denied", retryable=True, category="auth"),
    )
    assert denied.category == "auth" and not denied.retryable


def test_stream_truncation_relabel_and_provider_side_barrier():
    transport = classify_transport(RuntimeError("503 unavailable"), started=True)
    assert transport.category == "stream_truncated" and transport.retryable
    setup = classify_transport(RuntimeError("503 unavailable"), started=False)
    assert setup.category == "temporary" and setup.retryable
    barrier = classify_failure(ProviderError(
        "truncated after server tool", retryable=True,
        category="stream_truncated", server_side_activity=True,
    ))
    assert barrier.retryable is False and barrier.server_side_activity is True
    explicit = classify_transport(RuntimeError("rate_limit_error"), started=False)
    assert explicit.category == "rate_limited"


def test_provider_error_usage_and_retry_after_are_preserved():
    failure = classify_failure(ProviderError(
        "429 slow down", retryable=True, category="rate_limited",
        retry_after=3.25, usage={"input_tokens": 11},
    ))
    assert failure.retry_after == 3.25 and failure.usage == {"input_tokens": 11}


def test_preview_identity_resets_between_attempts():
    store = PreviewStore(limit=50, active_limit=8)
    store.append("run", "request-1", "first attempt draft", attempt_id="a1")
    assert store.get("run")["text"] == "first attempt draft"
    assert store.get("run")["attempt_id"] == "a1"
    store.append("run", "request-1", "second", attempt_id="a2")
    assert store.get("run")["text"] == "second"
    assert store.get("run")["attempt_id"] == "a2"
    # A call without an attempt identity is a different producer, not a
    # continuation of attempt a2.
    store.append("run", "request-1", " tail")
    assert store.get("run")["text"] == " tail"
    legacy = PreviewStore(limit=50, active_limit=8)
    legacy.append("run", "request-1", "chunk one ")
    legacy.append("run", "request-1", "chunk two")
    assert legacy.get("run")["text"] == "chunk one chunk two"
    legacy.append("run", "request-2", "new request")
    assert legacy.get("run")["text"] == "new request"


async def test_retry_recovers_and_reports_category_and_schedule(isolated_workspace):
    provider = ScriptedProvider([
        {"error": "temporary provider outage", "retryable": True, "category": "temporary"},
        {"text": "recovered", "usage": {"input_tokens": 4, "output_tokens": 2}},
    ])
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=0.01)
    host = RuntimeHost(settings, provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        retries = retry_events(host, result.run_id)
        assert len(retries) == 1
        payload = retries[0]
        assert payload["retryable"] is True
        assert payload["category"] == "temporary"
        assert payload["next_attempt"] == 2
        assert 0.01 <= payload["delay_seconds"] <= 0.02
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["attempt_count"] == 2
        assert ledger["unknown_attempts"] == 1
        assert ledger["reported_totals"] == {"input_tokens": 4, "output_tokens": 2}
    finally:
        await host.close()


async def test_rate_limit_honors_retry_after_before_next_attempt(isolated_workspace):
    provider = ScriptedProvider([
        {"error": "slow down", "retryable": True, "category": "rate_limited",
         "retry_after": 0.25},
        {"text": "after window"},
    ])
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=0.001)
    host = RuntimeHost(settings, provider)
    try:
        started = time.monotonic()
        result = await host.run(RunRequest("work"))
        elapsed = time.monotonic() - started
        assert result.status == "completed"
        assert elapsed >= 0.24
        payload = retry_events(host, result.run_id)[0]
        assert payload["retry_after_seconds"] == 0.25
        assert payload["delay_seconds"] >= 0.25
    finally:
        await host.close()


async def test_auth_failure_never_retries(isolated_workspace):
    provider = ScriptedProvider([
        {"error": "invalid api key", "retryable": True, "category": "auth"},
    ])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert len(provider.requests) == 1
        payload = retry_events(host, result.run_id)[0]
        assert payload["retryable"] is False
        assert payload["category"] == "auth"
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["attempt_count"] == 1 and ledger["unknown_attempts"] == 1
    finally:
        await host.close()


async def test_billing_failure_never_retries(isolated_workspace):
    provider = ScriptedProvider([{"error": "Insufficient Balance", "category": "billing"}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert len(provider.requests) == 1
        assert retry_events(host, result.run_id)[0]["category"] == "billing"
    finally:
        await host.close()


async def test_provider_side_tool_activity_blocks_replay(isolated_workspace):
    class BarrierProvider:
        calls = 0

        async def stream(self, request):
            self.calls += 1
            yield SimpleNamespace(kind="text_delta", text="searching the web", response=None)
            raise ProviderError(
                "stream cut after provider-executed search",
                retryable=True, category="stream_truncated", server_side_activity=True,
            )

    provider = BarrierProvider()
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=0.001)
    host = RuntimeHost(settings, provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert provider.calls == 1
        payload = retry_events(host, result.run_id)[0]
        assert payload["retryable"] is False
        assert payload["server_side_activity"] is True
        assert build_usage_ledger(
            host.store.usage_events(run_id=result.run_id),
        )["unknown_attempts"] == 1
    finally:
        await host.close()


async def test_step_fails_after_ten_attempts_with_backoff_only(isolated_workspace):
    provider = ScriptedProvider([
        {"error": "503 unavailable", "retryable": True, "category": "temporary"},
    ] * MODEL_STEP_ATTEMPTS + [{"text": "never reached"}])
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=0.001)
    host = RuntimeHost(settings, provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert len(provider.requests) == MODEL_STEP_ATTEMPTS
        retries = retry_events(host, result.run_id)
        assert len(retries) == MODEL_STEP_ATTEMPTS
        assert [item["attempt"] for item in retries] == list(range(1, MODEL_STEP_ATTEMPTS + 1))
        assert retries[-1]["retryable"] is False
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["unknown_attempts"] == MODEL_STEP_ATTEMPTS
    finally:
        await host.close()


async def test_retry_wait_limit_stops_before_more_attempts(isolated_workspace):
    provider = ScriptedProvider([
        {"error": "come back later", "retryable": True, "category": "rate_limited",
         "retry_after": 45.0},
        {"text": "never reached"},
    ])
    settings = replace(
        settings_for(isolated_workspace),
        model_retry_base_delay=0.001, model_retry_wait_limit=1.0,
    )
    host = RuntimeHost(settings, provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert len(provider.requests) == 1
        payload = retry_events(host, result.run_id)[0]
        assert payload["retryable"] is False
        assert payload["retry_wait_limit_exceeded"] is True
    finally:
        await host.close()


async def test_single_attempt_limit_still_applies(isolated_workspace):
    provider = ScriptedProvider([{"error": "503", "retryable": True}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        run_id = await host.admit_run(RunRequest("single"))
        with pytest.raises(ProviderError):
            await host._complete_with_retry(request(), run_id, None, max_attempts=1)
        assert len(provider.requests) == 1
        with pytest.raises(ValueError, match="Invalid model attempt limit"):
            await host._complete_with_retry(request(), run_id, None, max_attempts=11)
        assert len(provider.requests) == 1
    finally:
        await host.close()


async def test_cancellation_during_backoff_stops_without_further_attempts(isolated_workspace):
    provider = ScriptedProvider([{"error": "503", "retryable": True}])
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=30.0)
    host = RuntimeHost(settings, provider)
    try:
        run_id = await host.admit_run(RunRequest("work", run_id="cancel-backoff"))
        task = host.start_admitted_run(run_id)
        while not retry_events(host, run_id):
            await asyncio.sleep(0.01)
        await host.cancel_run(run_id)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(provider.requests) == 1
        assert host.store.get_run(run_id)["status"] == "interrupted"
        ledger = build_usage_ledger(host.store.usage_events(run_id=run_id))
        assert ledger["attempt_count"] == 1 and ledger["unknown_attempts"] == 1
    finally:
        await host.close()


async def test_truncated_tool_arguments_are_never_dispatched_across_retries(isolated_workspace):
    class TruncatedToolStream:
        calls = 0
        requests = []

        async def stream(self, request):
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                yield SimpleNamespace(
                    kind="text_delta", text='{"path": "report', response=None,
                )
                raise ProviderError(
                    "connection reset mid tool arguments", retryable=True,
                    category="stream_truncated",
                )
            yield SimpleNamespace(
                kind="response_completed",
                text="",
                response=ModelResponse("kept the draft", usage={"input_tokens": 6}),
            )

    provider = TruncatedToolStream()
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=0.001)
    host = RuntimeHost(settings, provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        assert provider.calls == 2
        events = host.store.run_events(result.run_id)
        assert not any(event["type"] == "tool.prepared" for event in events)
        retries = retry_events(host, result.run_id)
        assert retries[0]["category"] == "stream_truncated"
        assert provider.requests[1].messages == provider.requests[0].messages
        assert provider.requests[1].system == provider.requests[0].system
    finally:
        await host.close()


async def test_preview_events_carry_attempt_identity_and_never_mix(isolated_workspace):
    appended: list[dict] = []

    class RecordingPreviews(PreviewStore):
        def append(self, run_id, request_id, text, *, attempt_id=None):
            appended.append(
                {"request_id": request_id, "attempt_id": attempt_id, "text": text},
            )
            super().append(run_id, request_id, text, attempt_id=attempt_id)

    class TwoAttemptStream:
        calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield SimpleNamespace(kind="text_delta", text="first draft", response=None)
                raise ProviderError("connection lost", retryable=True)
            yield SimpleNamespace(kind="text_delta", text="second draft", response=None)
            yield SimpleNamespace(
                kind="response_completed",
                text="",
                response=ModelResponse("second draft", usage={"input_tokens": 2}),
            )

    provider = TwoAttemptStream()
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=0.001)
    host = RuntimeHost(settings, provider)
    host.previews = RecordingPreviews()
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        assert [item["text"] for item in appended] == ["first draft", "second draft"]
        assert appended[0]["attempt_id"] != appended[1]["attempt_id"]
        assert host.previews.get(result.run_id) is None
    finally:
        await host.close()


async def test_unwrapped_provider_exception_never_retries(isolated_workspace):
    class BrokenProvider:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            raise RuntimeError("adapter forgot to wrap this")

    provider = BrokenProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert provider.calls == 1
        assert not retry_events(host, result.run_id)
    finally:
        await host.close()


async def test_failed_attempt_usage_is_not_lost_when_provider_reported_tokens(
    isolated_workspace,
):
    provider = ScriptedProvider([
        {"error": "cut after usage", "retryable": True, "usage": {"input_tokens": 9}},
        {"text": "done", "usage": {"input_tokens": 1, "output_tokens": 2}},
    ])
    settings = replace(settings_for(isolated_workspace), model_retry_base_delay=0.001)
    host = RuntimeHost(settings, provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["attempt_count"] == 2
        assert ledger["reported_totals"] == {"input_tokens": 10, "output_tokens": 2}
        assert ledger["unknown_attempts"] == 0
        finished = [
            event["payload"]
            for event in host.store.run_events(result.run_id)
            if event["type"] == "model.attempt_finished"
        ]
        assert finished[0]["usage"] == {"input_tokens": 9}
        assert finished[0]["usage_source"] == "reported"
    finally:
        await host.close()
