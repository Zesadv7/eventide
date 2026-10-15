"""Protocol-level stream assembly without network or paid model requests."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from eventide.models import ModelRequest
from eventide.providers.anthropic import AnthropicProvider, _without_provider_state
from eventide.providers.base import ProviderError
from eventide.providers.openai_compatible import OpenAICompatibleProvider, convert_messages
from eventide.providers.openai_responses import OpenAIResponsesProvider, convert_response_input
from eventide.streaming import ModelStreamEvent


class FakeStream:
    def __init__(self, events, *, failure=None, wait=False):
        self.events = events
        self.failure = failure
        self.wait = wait
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for event in self.events:
            yield event
        if self.wait:
            self.waiting.set()
            await asyncio.Event().wait()
        if self.failure:
            raise self.failure

    async def close(self):
        self.closed = True


def provider(kind, events, **options):
    classes = {
        "responses": OpenAIResponsesProvider,
        "chat": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
    }
    instance = object.__new__(classes[kind])
    stream = FakeStream(events, **options)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return stream

    endpoint = SimpleNamespace(create=create)
    instance._client = SimpleNamespace(
        responses=endpoint, messages=endpoint, chat=SimpleNamespace(completions=endpoint),
    )
    return instance, stream, calls


def request():
    return ModelRequest("system", [{"role": "user", "content": "hi"}], [], "model", 32)


def response_final(**changes):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed", "output": [], "output_text": "hello",
            "usage": {"input_tokens": 10, "output_tokens": 3}, **changes,
        },
    }


def chat_chunk(*, content=None, reasoning=None, calls=None, finish=None, usage=None):
    return {
        "choices": [{
            "index": 0,
            "delta": {"content": content, "reasoning_content": reasoning, "tool_calls": calls},
            "finish_reason": finish,
        }],
        "usage": usage,
    }


def anthropic_events():
    return [
        {"type": "message_start", "message": {"usage": {
            "input_tokens": 5, "output_tokens": 0, "cache_read_input_tokens": 2,
        }}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "private"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "signed"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "c1", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '"a.py"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]


async def test_responses_incremental_text_terminal_tools_state_and_usage():
    output = [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "c1", "name": "read_file",
         "arguments": '{"path":"a.py"}'},
    ]
    instance, stream, calls = provider("responses", [
        {"type": "response.output_item.added", "output_index": 0, "item": output[0]},
        {"type": "response.reasoning_text.delta", "delta": "private"},
        {"type": "response.output_text.delta", "delta": "hel"},
        {"type": "response.function_call_arguments.delta", "delta": '{"path":'},
        {"type": "response.function_call_arguments.delta", "delta": '"a.py"}'},
        {"type": "response.output_text.delta", "delta": "lo"},
        response_final(output=output),
    ])
    events = [item async for item in instance.stream(request())]
    assert [item.text for item in events[:-1]] == ["hel", "lo"]
    assert all(item.response is None for item in events[:-1])
    result = events[-1].response
    assert result.text == "hello"
    assert result.tool_calls[0].arguments == {"path": "a.py"}
    assert result.usage == {"input_tokens": 10, "output_tokens": 3}
    replay = convert_response_input([{"role": "assistant", "content": result.content_blocks()}])
    assert replay[0] == output[0]
    assert stream.closed and len(calls) == 1
    assert calls[0]["stream"] is True and "tools" not in calls[0]


async def test_chat_interleaved_partial_tools_reasoning_and_cumulative_usage():
    instance, stream, calls = provider("chat", [
        chat_chunk(reasoning="private"),
        chat_chunk(content="hello", calls=[
            {"index": 1, "id": "c2", "function": {"name": "glob", "arguments": '{"pattern":'}},
            {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": '{"path":'}},
        ]),
        chat_chunk(calls=[
            {"index": 0, "function": {"arguments": '"a.py"}'}},
            {"index": 1, "function": {"arguments": '"*.py"}'}},
        ], finish="tool_calls"),
        {"choices": [], "usage": {
            "prompt_tokens": 10, "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 1},
        }},
    ])
    events = [item async for item in instance.stream(request())]
    assert events[0] == ModelStreamEvent("text_delta", text="hello")
    result = events[-1].response
    assert [call.id for call in result.tool_calls] == ["c1", "c2"]
    assert result.tool_calls[0].arguments == {"path": "a.py"}
    assert result.provider_items == ({"reasoning_content": "private"},)
    assert result.usage["input_tokens"] == 10
    assert result.usage["output_tokens"] == 3
    replay = convert_messages([{"role": "assistant", "content": result.content_blocks()}], "sys")
    assert replay[-1]["reasoning_content"] == "private"
    assert stream.closed and calls[0]["stream_options"] == {"include_usage": True}


async def test_anthropic_blocks_complete_only_at_message_stop_and_preserve_thinking():
    instance, stream, calls = provider("anthropic", anthropic_events())
    events = [item async for item in instance.stream(request())]
    assert len(events) == 2
    assert events[0] == ModelStreamEvent("text_delta", text="hello")
    result = events[-1].response
    assert result.tool_calls[0].arguments == {"path": "a.py"}
    assert result.provider_items == (
        {"type": "thinking", "thinking": "private", "signature": "signed"},
    )
    replay = _without_provider_state([{"role": "assistant", "content": result.content_blocks()}])
    assert replay[0]["content"][0] == result.provider_items[0]
    assert result.usage["input_tokens"] == 7  # fresh and cache inputs are disjoint here
    assert result.usage["output_tokens"] == 3
    assert stream.closed and calls[0]["stream"] is True


@pytest.mark.parametrize("kind,events", [
    ("responses", [{"type": "response.output_text.delta", "delta": "partial"}]),
    ("chat", [chat_chunk(content="partial")]),
    ("anthropic", anthropic_events()[:-1]),
])
async def test_missing_terminal_never_returns_a_complete_response(kind, events):
    instance, stream, calls = provider(kind, events)
    seen = []
    with pytest.raises(ProviderError) as caught:
        async for event in instance.stream(request()):
            seen.append(event)
    assert not caught.value.retryable
    assert all(event.response is None for event in seen)
    assert stream.closed and len(calls) == 1


@pytest.mark.parametrize("kind,events", [
    ("responses", [{"type": "response.output_text.delta", "delta": "partial"}]),
    ("chat", [chat_chunk(content="partial")]),
    ("anthropic", anthropic_events()[:7]),
])
async def test_partial_stream_failure_is_nonretryable_and_closes(kind, events):
    instance, stream, calls = provider(kind, events, failure=RuntimeError("503 unavailable"))
    with pytest.raises(ProviderError) as caught:
        _ = [event async for event in instance.stream(request())]
    assert not caught.value.retryable
    assert len(calls) == 1 and stream.closed


@pytest.mark.parametrize("kind,events", [
    ("responses", [{"type": "response.output_text.delta", "delta": "partial"}]),
    ("chat", [chat_chunk(content="partial")]),
    ("anthropic", anthropic_events()[:7]),
])
async def test_consumer_close_releases_transport(kind, events):
    instance, stream, _ = provider(kind, events)
    iterator = instance.stream(request())
    assert (await anext(iterator)).kind == "text_delta"
    await iterator.aclose()
    assert stream.closed


@pytest.mark.parametrize("kind,events", [
    ("responses", []), ("chat", []), ("anthropic", []),
])
async def test_cancellation_releases_waiting_transport(kind, events):
    instance, stream, _ = provider(kind, events, wait=True)
    task = asyncio.create_task(anext(instance.stream(request())))
    await stream.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


@pytest.mark.parametrize("kind,events", [
    ("responses", [{"type": "response.output_text.delta", "delta": "123456"}]),
    ("chat", [chat_chunk(reasoning="123456")]),
    ("anthropic", anthropic_events()),
])
async def test_stream_buffers_are_bounded(monkeypatch, kind, events):
    monkeypatch.setattr("eventide.streaming.MAX_STREAM_BYTES", 5)
    instance, stream, _ = provider(kind, events)
    with pytest.raises(ProviderError, match="byte limit"):
        _ = [event async for event in instance.stream(request())]
    assert stream.closed


@pytest.mark.parametrize("kind,events", [
    ("responses", [response_final(usage=None)]),
    ("chat", [chat_chunk(finish="stop")]),
    ("anthropic", [
        {"type": "message_start", "message": {}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]),
])
async def test_missing_usage_stays_unknown(kind, events):
    instance, stream, _ = provider(kind, events)
    events = [event async for event in instance.stream(request())]
    assert events[-1].response.usage == {}
    assert stream.closed


async def test_chat_invalid_json_is_not_treated_as_executable_partial_arguments():
    instance, _, _ = provider("chat", [chat_chunk(calls=[{
        "index": 0, "id": "c1", "function": {"name": "read_file", "arguments": '{"path":'},
    }], finish="tool_calls")])
    events = [event async for event in instance.stream(request())]
    assert len(events) == 1
    assert events[0].response.tool_calls[0].arguments == {"_invalid_json": '{"path":'}


async def test_responses_incomplete_keeps_reason_and_reported_usage():
    terminal = response_final(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"},
    )
    terminal["type"] = "response.incomplete"
    instance, stream, _ = provider("responses", [terminal])
    events = [event async for event in instance.stream(request())]
    assert events[-1].response.stop_reason == "max_output_tokens"
    assert events[-1].response.usage["output_tokens"] == 3
    assert stream.closed


@pytest.mark.parametrize("events", [
    [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}}],
    anthropic_events()[:2] + [{"type": "message_stop"}],
])
async def test_anthropic_rejects_invalid_event_order(events):
    instance, stream, _ = provider("anthropic", events)
    with pytest.raises(ProviderError):
        _ = [event async for event in instance.stream(request())]
    assert stream.closed


async def test_real_openai_sdk_parses_sse_without_network():
    import httpx
    from openai import AsyncOpenAI

    captured = []
    events = [
        {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
         **chat_chunk(content="hello")},
        {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
         **chat_chunk(finish="stop", usage={"prompt_tokens": 2, "completion_tokens": 1})},
    ]
    payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    async def handle(req):
        captured.append(json.loads(req.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    client = AsyncOpenAI(
        api_key="fake", base_url="https://example.invalid", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    instance = object.__new__(OpenAICompatibleProvider)
    instance._client = client
    try:
        seen = [event async for event in instance.stream(request())]
        assert seen[0].text == "hello"
        assert seen[-1].response.usage == {"input_tokens": 2, "output_tokens": 1}
        assert captured[0]["stream"] is True
    finally:
        await instance.close()


async def test_real_responses_sdk_terminal_object_preserves_reasoning_and_tools():
    import httpx
    from openai import AsyncOpenAI

    output = [
        {"id": "r1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
        {"id": "m1", "type": "message", "status": "completed", "role": "assistant",
         "content": [{"type": "output_text", "text": "hello", "annotations": []}]},
        {"id": "f1", "type": "function_call", "status": "completed", "call_id": "c1",
         "name": "read_file", "arguments": '{"path":"a.py"}'},
    ]
    events = [
        {"type": "response.output_text.delta", "sequence_number": 1,
         "output_index": 1, "content_index": 0, "item_id": "m1", "delta": "hello"},
        {"type": "response.completed", "sequence_number": 2, "response": {
            "id": "r", "object": "response", "created_at": 0, "model": "m",
            "status": "completed", "output": output,
            "usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
        }},
    ]
    payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

    async def handle(_req):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    instance = object.__new__(OpenAIResponsesProvider)
    instance._client = AsyncOpenAI(
        api_key="fake", base_url="https://example.invalid", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        events = [event async for event in instance.stream(request())]
        assert events[0].text == "hello"
        assert events[-1].response.text == "hello"
        assert events[-1].response.tool_calls[0].arguments == {"path": "a.py"}
        assert events[-1].response.provider_items[0]["encrypted_content"] == "opaque"
        assert events[-1].response.usage == {"input_tokens": 4, "output_tokens": 5}
    finally:
        await instance.close()


async def test_real_anthropic_sdk_parses_raw_block_events_without_network():
    from anthropic import AsyncAnthropic, _base_client

    # Anthropic SDK 0.x uses httpx; 1.x uses httpx2. Reuse its transport module
    # so this protocol test covers both supported dependency families.
    http_module = getattr(_base_client, "httpx2", None) or _base_client.httpx
    events = anthropic_events()
    events[0]["message"].update({
        "id": "m1", "type": "message", "role": "assistant", "model": "m",
        "content": [], "stop_reason": None, "stop_sequence": None,
    })
    events[-2]["delta"]["stop_sequence"] = None
    payload = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    )

    async def handle(_req):
        return http_module.Response(
            200, headers={"content-type": "text/event-stream"}, content=payload,
        )

    instance = object.__new__(AnthropicProvider)
    instance._client = AsyncAnthropic(
        api_key="fake", base_url="https://example.invalid", max_retries=0,
        http_client=http_module.AsyncClient(transport=http_module.MockTransport(handle)),
    )
    try:
        events = [event async for event in instance.stream(request())]
        assert events[0].text == "hello"
        assert events[-1].response.tool_calls[0].arguments == {"path": "a.py"}
        assert events[-1].response.provider_items[0]["signature"] == "signed"
        assert events[-1].response.usage["output_tokens"] == 3
    finally:
        await instance.close()


@pytest.mark.parametrize("kind", ["responses", "chat", "anthropic"])
async def test_stream_setup_retryability_is_separate_from_partial_stream_failure(kind):
    instance, _, _ = provider(kind, [])

    async def fail(**_kwargs):
        raise RuntimeError("503 unavailable")

    instance._client.responses.create = fail
    with pytest.raises(ProviderError) as caught:
        _ = [event async for event in instance.stream(request())]
    assert caught.value.retryable


@pytest.mark.parametrize("kind,events", [
    ("responses", [{"type": "response.completed"}]),
    ("responses", [{"type": "response.failed"}]),
    ("chat", [chat_chunk(finish="stop"), chat_chunk(content="unexpected")]),
    ("chat", [chat_chunk(calls=[{"index": -1}], finish="tool_calls")]),
    ("chat", [chat_chunk(calls=[{"index": 0}], finish="tool_calls")]),
    ("anthropic", anthropic_events()[:2] + [{
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": "{}"},
    }]),
])
async def test_invalid_terminal_and_tool_identity_never_complete(kind, events):
    instance, stream, _ = provider(kind, events)
    with pytest.raises(ProviderError):
        _ = [event async for event in instance.stream(request())]
    assert stream.closed


async def test_empty_event_flood_is_bounded(monkeypatch):
    monkeypatch.setattr("eventide.streaming.MAX_STREAM_EVENTS", 2)
    instance, stream, _ = provider("chat", [chat_chunk() for _ in range(3)])
    with pytest.raises(ProviderError, match="event count limit"):
        _ = [event async for event in instance.stream(request())]
    assert stream.closed
