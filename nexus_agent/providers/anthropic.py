"""Anthropic Messages API adapter."""

from __future__ import annotations

from typing import Any, cast

from nexus_agent.config import Settings
from nexus_agent.models import ModelRequest, ModelResponse, ToolCall
from nexus_agent.providers.base import ProviderError


def _without_provider_state(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = [
                block
                for block in content
                if not isinstance(block, dict) or block.get("type") != "provider_state"
            ]
        cleaned.append({**message, "content": content})
    return cleaned


class AnthropicProvider:
    def __init__(self, settings: Settings):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("The anthropic package is required for live Anthropic use") from exc
        self._client = AsyncAnthropic(
            api_key=settings.require_api_key(),
            base_url=settings.base_url,
        )

    async def close(self) -> None:
        await self._client.close()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await self._client.messages.create(
                model=request.model,
                system=request.system,
                messages=cast(Any, _without_provider_state(request.messages)),
                tools=cast(Any, request.tools),
                max_tokens=request.max_tokens,
            )
        except Exception as exc:
            text = str(exc).lower()
            retryable = any(token in text for token in ("429", "529", "overloaded"))
            raise ProviderError(str(exc), retryable=retryable) from exc
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            raw_block: Any = block
            if getattr(raw_block, "type", "") == "text":
                text_parts.append(raw_block.text)
            elif getattr(raw_block, "type", "") == "tool_use":
                calls.append(ToolCall(raw_block.id, raw_block.name, dict(raw_block.input)))
        usage_obj: Any = getattr(response, "usage", None)
        usage = {
            "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
        }
        return ModelResponse("\n".join(text_parts), tuple(calls), str(response.stop_reason), usage)
