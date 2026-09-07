"""Live provider adapters tested with in-memory client doubles."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nexus_agent.config import Settings
from nexus_agent.models import ModelRequest
from nexus_agent.providers.anthropic import AnthropicProvider
from nexus_agent.providers.base import ProviderError, build_provider
from nexus_agent.providers.openai_compatible import OpenAICompatibleProvider


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


def test_provider_factory_and_missing_key(isolated_workspace):
    with pytest.raises(ValueError, match="Unsupported"):
        build_provider(live_settings(isolated_workspace, "other"))
    settings = live_settings(isolated_workspace, "anthropic")
    settings = replace(settings, api_key=None)
    with pytest.raises(RuntimeError, match="NEXUS_API_KEY"):
        settings.require_api_key()
