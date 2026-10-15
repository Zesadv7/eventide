"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

from eventide.config import Settings
from eventide.generation import wire_parameters
from eventide.models import ModelRequest, ModelResponse, ToolCall
from eventide.providers.base import ProviderError
from eventide.streaming import (
    MAX_STREAM_ITEMS,
    ModelStreamEvent,
    StreamLimit,
    closing_stream,
    field,
    parse_arguments,
    stream_error,
    to_dict,
)
from eventide.usage import normalize_provider_usage


def _without_provider_state(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            blocks: list[Any] = []
            for block in content:
                if not isinstance(block, dict):
                    blocks.append(block)
                elif block.get("type") == "provider_state":
                    if block.get("provider") == "anthropic":
                        blocks.extend(
                            item for item in block.get("items", [])
                            if isinstance(item, dict)
                            and item.get("type") in {"thinking", "redacted_thinking"}
                        )
                    continue
                elif block.get("type") == "image":
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": block["media_type"],
                                "data": block["data"],
                            },
                        }
                    )
                elif block.get("type") == "file":
                    blocks.append(
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": block["media_type"],
                                "data": block["data"],
                            },
                            "title": block.get("name") or "attachment.pdf",
                        }
                    )
                else:
                    blocks.append(block)
            content = blocks
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
            max_retries=0,
        )

    async def close(self) -> None:
        await self._client.close()

    def _parameters(self, request: ModelRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "system": request.system,
            "messages": cast(Any, _without_provider_state(request.messages)),
            "max_tokens": request.max_tokens,
        }
        params.update(wire_parameters("anthropic", request.generation))
        if request.tools:
            params["tools"] = cast(Any, request.tools)
        return params

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await cast(Any, self._client).messages.create(**self._parameters(request))
        except Exception as exc:
            raise stream_error(exc, started=False) from exc
        return parse_message(response)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = False
        message_started = False
        message_stopped = False
        stop_reason: str | None = None
        limit = StreamLimit()
        blocks: dict[int, dict[str, Any]] = {}
        open_blocks: set[int] = set()
        json_parts: dict[int, list[str]] = {}
        usage: dict[str, Any] = {}
        try:
            raw = await cast(Any, self._client).messages.create(
                **self._parameters(request), stream=True,
            )
            started = True
            async with closing_stream(raw):
                async for event in raw:
                    limit.event()
                    kind = field(event, "type", "")
                    if kind == "message_start":
                        if message_started:
                            raise ProviderError("Model stream repeated message_start")
                        message_started = True
                        message = field(event, "message")
                        usage.update(to_dict(field(message, "usage")) or {})
                        # The start value is provisional; only the final cumulative
                        # message_delta establishes actual output usage.
                        usage.pop("output_tokens", None)
                    elif kind == "ping":
                        continue
                    elif not message_started:
                        raise ProviderError("Model stream event arrived before message_start")
                    elif kind == "content_block_start":
                        index = field(event, "index")
                        if type(index) is not int or not 0 <= index < MAX_STREAM_ITEMS:
                            raise ProviderError("Invalid model stream content block index")
                        if index in blocks:
                            raise ProviderError("Model stream repeated a content block")
                        block = to_dict(field(event, "content_block"))
                        if not isinstance(block, dict):
                            raise ProviderError("Invalid model stream content block")
                        limit.add(json.dumps(block, ensure_ascii=False))
                        blocks[index] = block
                        open_blocks.add(index)
                        if block.get("type") == "text" and block.get("text"):
                            yield ModelStreamEvent("text_delta", text=str(block["text"]))
                    elif kind == "content_block_delta":
                        index = field(event, "index")
                        if index not in open_blocks:
                            raise ProviderError("Model stream delta has no open content block")
                        block = blocks[index]
                        delta = field(event, "delta")
                        delta_kind = field(delta, "type")
                        if delta_kind == "input_json_delta":
                            if block.get("type") != "tool_use":
                                raise ProviderError("Tool JSON delta mismatched its content block")
                            part = limit.add(str(field(delta, "partial_json", "")))
                            json_parts.setdefault(index, []).append(part)
                        else:
                            keys = {
                                "text_delta": ("text", "text"),
                                "thinking_delta": ("thinking", "thinking"),
                                "signature_delta": ("signature", "thinking"),
                            }
                            if delta_kind not in keys:
                                continue
                            key, expected = keys[delta_kind]
                            if block.get("type") != expected:
                                raise ProviderError(
                                    "Model stream delta mismatched its content block",
                                )
                            part = limit.add(str(field(delta, key, "")))
                            block[key] = str(block.get(key, "")) + part
                            if delta_kind == "text_delta" and part:
                                yield ModelStreamEvent("text_delta", text=part)
                    elif kind == "content_block_stop":
                        index = field(event, "index")
                        if index not in open_blocks:
                            raise ProviderError("Model stream stopped an unknown content block")
                        open_blocks.remove(index)
                        if index in json_parts:
                            blocks[index]["input"] = parse_arguments("".join(json_parts[index]))
                    elif kind == "message_delta":
                        delta = field(event, "delta")
                        reason = field(delta, "stop_reason")
                        if reason is not None:
                            stop_reason = str(reason)
                        # Message deltas carry cumulative totals, not increments.
                        values = to_dict(field(event, "usage")) or {}
                        usage.update({
                            key: value for key, value in values.items() if value is not None
                        })
                    elif kind == "message_stop":
                        if open_blocks or stop_reason is None:
                            raise ProviderError(
                                "Model stream stopped before completing its content",
                            )
                        message_stopped = True
                        break
                    elif kind == "error":
                        raise ProviderError("Model message stream failed")
            if not message_stopped:
                raise ProviderError("Model stream ended without message_stop")
            result = parse_message({
                "content": [block for _, block in sorted(blocks.items())],
                "usage": usage, "stop_reason": stop_reason,
            })
        except Exception as exc:
            raise stream_error(exc, started=started) from exc
        yield ModelStreamEvent("response_completed", response=result)


def parse_message(response: Any) -> ModelResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    state: list[dict[str, Any]] = []
    for block in field(response, "content", []) or []:
        kind = field(block, "type", "")
        if kind == "text":
            text_parts.append(str(field(block, "text", "")))
        elif kind == "tool_use":
            calls.append(ToolCall(
                str(field(block, "id", "")), str(field(block, "name", "")),
                dict(field(block, "input", {})),
            ))
        elif kind in {"thinking", "redacted_thinking"}:
            state.append(to_dict(block))
    usage = normalize_provider_usage("anthropic", field(response, "usage"))
    return ModelResponse(
        "\n".join(text_parts), tuple(calls), str(field(response, "stop_reason", "end_turn")),
        usage, tuple(state), "anthropic",
    )
