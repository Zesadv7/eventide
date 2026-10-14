"""Summary responses are bounded, audited and charged once, including rejection."""

import asyncio

import pytest

from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from tests.test_runtime import COMPACTION_BUDGET, settings_for


async def test_summary_usage_is_recorded_once_and_never_becomes_a_message(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"text": "x" * 5000},
            {"text": "compact prior work", "usage": {"input_tokens": 5, "output_tokens": 7}},
            {"text": "done", "usage": {"input_tokens": 3, "output_tokens": 4}},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace, context_limit=COMPACTION_BUDGET), provider)
    try:
        first = await host.run(RunRequest("prior"))
        result = await host.run(RunRequest("next", first.session_id))
        assert result.status == "completed"
        assert result.usage == {"input_tokens": 8, "output_tokens": 11}
        assert host.store.get_run(result.run_id)["usage"] == result.usage
        events = host.store.run_events(result.run_id)
        compacted = next(e for e in events if e["type"] == "context.compacted")
        assert "usage" not in compacted["payload"]
        generated = next(e for e in events if e["type"] == "context.summary_generated")
        assert "message" not in generated["payload"]
        assert len([e for e in events if e["type"] == "model.response"]) == 1
        assert all(
            message["content"] != "compact prior work"
            for message in host.store.load_messages(first.session_id)
        )
    finally:
        await host.close()


async def test_rejected_summary_usage_survives_context_overflow(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"text": "x" * 5000},
            {
                "text": "partial",
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 9, "output_tokens": 3},
            },
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace, context_limit=COMPACTION_BUDGET), provider)
    try:
        first = await host.run(RunRequest("prior"))
        result = await host.run(RunRequest("next", first.session_id))
        assert result.status == "failed"
        assert "context_overflow" in result.output
        assert result.usage == {"input_tokens": 9, "output_tokens": 3}
        assert host.store.get_run(result.run_id)["usage"] == result.usage
        assert host.store.checkpoints(first.session_id) == []
    finally:
        await host.close()


async def test_summary_obeys_model_timeout_and_retry_limit(isolated_workspace):
    class Provider:
        def __init__(self):
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelResponse("x" * 5000)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    provider = Provider()
    host = RuntimeHost(
        settings_for(
            isolated_workspace,
            context_limit=COMPACTION_BUDGET,
            model_timeout=0.01,
        ),
        provider,
    )
    try:
        first = await host.run(RunRequest("prior"))
        result = await asyncio.wait_for(host.run(RunRequest("next", first.session_id)), 2)
        assert result.status == "failed"
        assert len(provider.requests) == 4
        retries = [
            e["payload"] for e in host.store.run_events(result.run_id) if e["type"] == "model.retry"
        ]
        assert [item["kind"] for item in retries] == ["context_summary"] * 3
    finally:
        await host.close()


async def test_summary_token_limit_parks_before_another_model_call(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"text": "x" * 5000},
            {"text": "compact prior work", "usage": {"input_tokens": 8, "output_tokens": 5}},
            {"text": "continued", "usage": {"input_tokens": 2, "output_tokens": 1}},
        ]
    )
    host = RuntimeHost(
        settings_for(
            isolated_workspace,
            context_limit=COMPACTION_BUDGET,
            run_token_limit=10,
        ),
        provider,
    )
    try:
        first = await host.run(RunRequest("prior"))
        parked = await host.run(RunRequest("next", first.session_id))
        assert parked.status == "interrupted"
        assert host.store.get_run(parked.run_id)["reason"] == "token_budget"
        assert parked.usage == {"input_tokens": 8, "output_tokens": 5}
        assert parked.steps == 0
        assert len(provider.requests) == 2
    finally:
        await host.close()


def test_token_limit_from_env_and_negative_values(isolated_workspace, monkeypatch):
    monkeypatch.setenv("EVENTIDE_RUN_TOKEN_LIMIT", "123")
    assert Settings.from_env(isolated_workspace).run_token_limit == 123
    with pytest.raises(ValueError, match="zero or positive"):
        settings_for(isolated_workspace, run_token_limit=-1)
