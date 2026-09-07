"""Live provider adapters tested with in-memory client doubles."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nexus_agent.config import Settings
from nexus_agent.models import ModelRequest
from nexus_agent.providers.anthropic import AnthropicProvider, _without_provider_state
from nexus_agent.providers.base import ProviderError, build_provider
from nexus_agent.providers.openai_compatible import OpenAICompatibleProvider
from nexus_agent.providers.openai_responses import (
    OpenAIResponsesProvider,
    convert_response_input,
)


def live_settings(isolated_workspace, provider):
    return Settings(
        workdir=isolated_workspace,
        state_dir=isolated_workspace / ".nexus",
        provider=provider,
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="test-model",
        fallback_model=None,
    )


def request():
    return ModelRequest("system", [{"role": "user", "content": "hello"}], [], "model", 32)


async def test_anthropic_adapter_response_and_retryable_error(isolated_workspace):
    provider = AnthropicProvider(live_settings(isolated_workspace, "anthropic"))

    class Messages:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="hello"),
                    SimpleNamespace(
                        type="tool_use", id="c1", name="read_file", input={"path": "x"}
                    ),
                ],
                usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                stop_reason="tool_use",
            )

    provider._client = SimpleNamespace(messages=Messages())
    result = await provider.complete(request())
    assert result.text == "hello" and result.tool_calls[0].name == "read_file"
    assert result.usage == {"input_tokens": 2, "output_tokens": 3}

    class FailingMessages:
        async def create(self, **_kwargs):
            raise RuntimeError("429 overloaded")

    provider._client = SimpleNamespace(messages=FailingMessages())
    with pytest.raises(ProviderError) as caught:
        await provider.complete(request())
    assert caught.value.retryable


async def test_openai_adapter_response_invalid_json_and_error(isolated_workspace):
    provider = OpenAICompatibleProvider(live_settings(isolated_workspace, "openai-compatible"))

    class Completions:
        async def create(self, **_kwargs):
            function = SimpleNamespace(name="read_file", arguments="not-json")
            message = SimpleNamespace(
                content="checking", tool_calls=[SimpleNamespace(id="c1", function=function)]
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=5),
            )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    result = await provider.complete(request())
    assert result.tool_calls[0].arguments["_invalid_json"] == "not-json"
    assert result.usage["output_tokens"] == 5

    class FailingCompletions:
        async def create(self, **_kwargs):
            raise RuntimeError("503 unavailable")

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FailingCompletions())
    )
    with pytest.raises(ProviderError) as caught:
        await provider.complete(request())
    assert caught.value.retryable


async def test_openai_responses_adapter_tool_roundtrip_and_usage(isolated_workspace):
    provider = OpenAIResponsesProvider(live_settings(isolated_workspace, "openai_responses"))
    captured = {}

    class Responses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            class Reasoning:
                type = "reasoning"

                def model_dump(self, **_kwargs):
                    return {"type": "reasoning", "encrypted_content": "opaque-state"}

            call = SimpleNamespace(
                type="function_call", call_id="call_1", name="read_file", arguments='{"path":"x"}'
            )
            return SimpleNamespace(
                output=[Reasoning(), call],
                output_text="正在读取",
                usage=SimpleNamespace(input_tokens=6, output_tokens=7),
                status="completed",
            )

    provider._client = SimpleNamespace(responses=Responses())
    model_request = ModelRequest(
        "system",
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "old",
                        "name": "bash",
                        "input": {"command": "pwd"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "old", "content": "ok"}
                ],
            },
        ],
        [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}],
        "model",
        64,
    )
    result = await provider.complete(model_request)
    assert captured["store"] is False
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert captured["input"][0]["type"] == "function_call"
    assert captured["input"][1] == {
        "type": "function_call_output", "call_id": "old", "output": "ok"
    }
    assert captured["tools"][0]["name"] == "read_file"
    assert result.tool_calls[0].arguments == {"path": "x"}
    assert result.usage == {"input_tokens": 6, "output_tokens": 7}
    replay = convert_response_input(
        [{"role": "assistant", "content": result.content_blocks()}]
    )
    assert replay[0] == {"type": "reasoning", "encrypted_content": "opaque-state"}


async def test_openai_responses_adapter_invalid_json_and_retryable_error(isolated_workspace):
    provider = OpenAIResponsesProvider(live_settings(isolated_workspace, "openai_responses"))

    class InvalidResponses:
        async def create(self, **_kwargs):
            call = SimpleNamespace(
                type="function_call", call_id="call_1", name="read_file", arguments="bad-json"
            )
            return SimpleNamespace(output=[call], output_text="", usage=None, status="completed")

    provider._client = SimpleNamespace(responses=InvalidResponses())
    result = await provider.complete(request())
    assert result.tool_calls[0].arguments["_invalid_json"] == "bad-json"

    class FailingResponses:
        async def create(self, **_kwargs):
            raise RuntimeError("503 unavailable")

    provider._client = SimpleNamespace(responses=FailingResponses())
    with pytest.raises(ProviderError) as caught:
        await provider.complete(request())
    assert caught.value.retryable


def test_provider_factory_and_missing_key(isolated_workspace):
    with pytest.raises(ValueError, match="Unsupported"):
        build_provider(live_settings(isolated_workspace, "other"))
    settings = live_settings(isolated_workspace, "anthropic")
    settings = replace(settings, api_key=None)
    with pytest.raises(RuntimeError, match="NEXUS_API_KEY"):
        settings.require_api_key()


def test_anthropic_messages_drop_foreign_provider_state():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "provider_state", "provider": "openai_responses", "items": []},
                {"type": "text", "text": "hello"},
            ],
        }
    ]
    assert _without_provider_state(messages)[0]["content"] == [
        {"type": "text", "text": "hello"}
    ]
