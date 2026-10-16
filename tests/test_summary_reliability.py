"""Summary completion ownership survives rejected text, overflow and cancellation."""

import asyncio
import json

import pytest

from eventide.context_builder import (
    SUMMARY_CLAIM,
    ContextBuilder,
    ContextOverflow,
    request_size,
)
from eventide.models import ModelResponse, ToolCall
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from eventide.workspace import digest


@pytest.fixture
def history(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    store.create_run("prior", "session")
    store.append_message("session", {"role": "user", "content": "old goal " + "x" * 5000})
    store.finish_run("prior", status="completed", output="prior answer")
    store.create_run("active", "session")
    store.append_message("session", {"role": "user", "content": "current goal"})
    try:
        yield store
    finally:
        store.close()


async def test_owner_completion_accounts_once_before_adopted_summary_is_cached(history):
    provider = ScriptedProvider([{"text": "wrong completion"}])
    requests = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    async def complete_summary(request):
        requests.append(request)
        response = ModelResponse("retained goal", usage={"input_tokens": 7, "output_tokens": 2})
        for key, value in response.usage.items():
            usage[key] += value
        return response

    builder = ContextBuilder(history)
    before = history.session_events("session")
    messages, compacted, _ = await builder.build(
        "session", provider=provider, provider_name="scripted", model="selected-model",
        budget=1000, complete_summary=complete_summary,
    )
    assert messages[0]["content"] == "[Prior context]\nretained goal"
    assert messages[-1]["content"] == "current goal"
    assert compacted["usage"] == usage == {"input_tokens": 7, "output_tokens": 2}
    assert requests[0].model == "selected-model"
    assert requests[0].tools == []
    assert "old goal" in requests[0].messages[0]["content"]
    assert "current goal" not in requests[0].messages[0]["content"]
    assert provider.requests == []
    after = history.session_events("session")
    assert after[: len(before)] == before
    assert [event["type"] for event in after[len(before) :]] == [SUMMARY_CLAIM]

    cached, compacted_again, _ = await builder.build(
        "session", provider=provider, provider_name="scripted", model="selected-model",
        budget=1000, complete_summary=complete_summary,
    )
    assert cached == messages
    assert compacted_again is None
    assert len(requests) == 1
    assert usage == {"input_tokens": 7, "output_tokens": 2}


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse("", usage={"input_tokens": 7, "output_tokens": 2}),
        ModelResponse(
            "unusable", tool_calls=(ToolCall("call", "read_file", {"path": "data.txt"}),),
            usage={"input_tokens": 7, "output_tokens": 2},
        ),
        ModelResponse(
            "partial", stop_reason="max_tokens", usage={"input_tokens": 7, "output_tokens": 2},
        ),
    ],
    ids=["empty", "unexpected-tool", "truncated"],
)
async def test_rejected_summary_still_reaches_owner_accounting(history, response):
    received_usage = []

    async def complete_summary(request):
        received_usage.append(response.usage)
        return response

    messages, compacted, _ = await ContextBuilder(history).build(
        "session", provider=ScriptedProvider([]), provider_name="scripted", model="scripted",
        budget=10000, force=True, complete_summary=complete_summary,
    )
    assert messages[0]["content"].startswith("old goal")
    assert compacted["covered_seq"] == 0
    assert received_usage == [{"input_tokens": 7, "output_tokens": 2}]
    assert history.checkpoints("session") == []


async def test_unadopted_summary_is_accounted_when_tool_folding_fits(history):
    received_usage = []
    for index in range(4):
        call_id = f"call-{index}"
        history.append_event("active", "model.response", {"message": {
            "role": "assistant", "content": [ToolCall(
                call_id, "read_file", {"path": "data.txt"},
            ).as_block()],
        }})
        history.append_event("active", "tool.completed", {
            "call_id": call_id, "name": "read_file", "is_error": False,
            "content": "x" * 8000 if index == 0 else "short",
        })

    async def complete_summary(request):
        response = ModelResponse("z" * 20000, usage={"input_tokens": 19, "output_tokens": 2000})
        received_usage.append(response.usage)
        return response

    before = history.session_events("session")
    messages, compacted, trimmed = await ContextBuilder(history).build(
        "session", provider=ScriptedProvider([]), provider_name="scripted", model="scripted",
        budget=7500, complete_summary=complete_summary,
    )
    assert messages[0]["content"].startswith("old goal")
    assert request_size("", messages, []) <= 7500
    assert compacted is None
    assert trimmed["call_ids"] == ["call-0"]
    assert received_usage == [{"input_tokens": 19, "output_tokens": 2000}]
    assert history.checkpoints("session") == []
    after = history.session_events("session")
    assert after[: len(before)] == before
    assert [event["type"] for event in after[len(before) :]] == [SUMMARY_CLAIM]

    repeated, _, repeated_trimmed = await ContextBuilder(history).build(
        "session", provider=ScriptedProvider([]), provider_name="scripted", model="scripted",
        budget=7500, complete_summary=complete_summary,
    )
    assert repeated == messages
    assert repeated_trimmed["summary_skipped"] == "same_completed_prefix"
    assert received_usage == [{"input_tokens": 19, "output_tokens": 2000}]


async def test_owner_keeps_usage_when_context_overflows_after_summary(history):
    history.append_message("session", {"role": "user", "content": "active details " + "y" * 5000})
    received_usage = []

    async def complete_summary(request):
        response = ModelResponse("retained goal", usage={"input_tokens": 23, "output_tokens": 3})
        received_usage.append(response.usage)
        return response

    before = history.session_events("session")
    with pytest.raises(ContextOverflow, match="active turn still exceeds"):
        await ContextBuilder(history).build(
            "session", provider=ScriptedProvider([]), provider_name="scripted", model="scripted",
            budget=1000, complete_summary=complete_summary,
        )
    assert received_usage == [{"input_tokens": 23, "output_tokens": 3}]
    assert history.checkpoints("session") == []
    after = history.session_events("session")
    assert after[: len(before)] == before
    assert [event["type"] for event in after[len(before) :]] == [SUMMARY_CLAIM]


async def test_owner_failure_preserves_valid_checkpoint(history):
    events = history.session_events("session")
    boundary = next(event["session_seq"] for event in events if event["type"] == "run.completed")
    prefix = [event for event in events if event["session_seq"] <= boundary]
    history.save_checkpoint(
        "session", boundary, digest(prefix), "retained goal", "scripted", "scripted",
    )
    history.finish_run("active", status="completed", output="active answer")
    provider = ScriptedProvider([{"text": "must not bypass owner"}])
    requests = []

    async def complete_summary(request):
        requests.append(request)
        raise RuntimeError("summary request exhausted retries")

    before = history.checkpoints("session")
    messages, compacted, _ = await ContextBuilder(history).build(
        "session", provider=provider, provider_name="scripted", model="scripted",
        budget=1000, force=True, complete_summary=complete_summary,
    )
    assert messages[0]["content"] == "[Prior context]\nretained goal"
    assert messages[-1]["content"] == "current goal"
    assert compacted["covered_seq"] == 0
    assert len(requests) == 1
    assert provider.requests == []
    assert history.checkpoints("session") == before


async def test_cancellation_propagates_out_of_owner_summary_completion(history):
    entered = asyncio.Event()

    async def complete_summary(request):
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(ContextBuilder(history).build(
        "session", provider=ScriptedProvider([]), provider_name="scripted", model="scripted",
        budget=1000, complete_summary=complete_summary,
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert history.checkpoints("session") == []


async def test_direct_provider_completion_retains_usage_for_existing_consumers(history):
    provider = ScriptedProvider([{
        "text": "retained goal", "usage": {"input_tokens": 7, "output_tokens": 2},
    }])
    messages, compacted, _ = await ContextBuilder(history).build(
        "session", provider=provider, provider_name="scripted", model="scripted", budget=1000,
    )
    assert compacted["usage"] == {"input_tokens": 7, "output_tokens": 2}
    assert len(provider.requests) == 1
    assert "retained goal" in json.dumps(messages)
