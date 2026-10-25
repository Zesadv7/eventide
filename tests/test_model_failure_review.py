"""Independent review: distinguish billing exhaustion from transient throttling."""

from eventide.model_failures import classify_failure


class ProviderStatusError(Exception):
    def __init__(self, status_code, body, message):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_structured_billing_429_is_not_transient_rate_limiting():
    error = ProviderStatusError(
        429,
        {"error": {"code": "insufficient_quota", "type": "insufficient_quota"}},
        "Request failed",
    )
    result = classify_failure(error)
    assert result.category == "billing"
    assert not result.retryable


def test_actual_rate_limiting_remains_recoverable():
    error = ProviderStatusError(
        429, {"error": {"code": "rate_limit_exceeded"}}, "Too many requests"
    )
    result = classify_failure(error)
    assert result.category == "rate_limited"
    assert result.retryable


def test_numeric_fragments_inside_unclassified_errors_do_not_create_retry_authority():
    error = RuntimeError("Unexpected data in field order: 5001")
    result = classify_failure(error)
    assert result.category == "unknown"
    assert not result.retryable


def test_retry_after_alone_does_not_turn_unknown_failure_into_rate_limit():
    from types import SimpleNamespace

    error = RuntimeError("Unrecognized protocol state")
    error.response = SimpleNamespace(headers={"retry-after": "3"})
    assert not classify_failure(error).retryable


async def test_request_timeout_does_not_exhaust_the_backoff_wait_budget(isolated_workspace):
    import asyncio
    from dataclasses import replace

    from eventide.host import RuntimeHost
    from eventide.models import ModelResponse, RunRequest
    from tests.test_runtime import settings_for

    class TimedOnce:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(60)
            return ModelResponse("recovered")

    provider = TimedOnce()
    host = RuntimeHost(
        replace(
            settings_for(isolated_workspace),
            model_timeout=0.02,
            model_retry_base_delay=0.001,
            model_retry_wait_limit=0.01,
        ),
        provider,
    )
    try:
        result = await host.run(RunRequest("recover"))
        assert result.status == "completed"
        assert provider.calls == 2
    finally:
        await host.close()


async def test_hosted_tool_activity_is_rejected_before_a_stream_can_stall():
    import asyncio

    import pytest

    from eventide.providers import ProviderError
    from tests.test_provider_streaming import provider, request

    for kind, events in [
        (
            "responses",
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "web_search_call", "id": "hosted"},
                }
            ],
        ),
        (
            "anthropic",
            [
                {"type": "message_start", "message": {"usage": {}}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "server_tool_use", "id": "hosted"},
                },
            ],
        ),
    ]:
        instance, stream, calls = provider(kind, events, wait=True)

        async def collect(current):
            return [event async for event in current.stream(request())]

        with pytest.raises(ProviderError) as caught:
            await asyncio.wait_for(collect(instance), timeout=1)
        assert caught.value.server_side_activity
        assert not caught.value.retryable
        assert not stream.waiting.is_set()
        assert stream.closed
        assert len(calls) == 1
