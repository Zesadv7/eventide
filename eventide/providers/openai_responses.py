"""OpenAI Responses API adapter."""

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


def convert_response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": item["name"],
            "description": item.get("description", ""),
            "parameters": item.get("input_schema", {"type": "object", "properties": {}}),
        }
        for item in tools
    ]


def convert_response_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        if role == "user" and any(
            isinstance(block, dict) and block.get("type") in {"image", "file"}
            for block in content
        ):
            parts: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append({"type": "input_text", "text": str(block.get("text", ""))})
                elif block.get("type") == "image":
                    parts.append(
                        {
                            "type": "input_image",
                            "image_url": f"data:{block['media_type']};base64,{block['data']}",
                        }
                    )
                elif block.get("type") == "file":
                    parts.append(
                        {
                            "type": "input_file",
                            "filename": block.get("name") or "attachment.pdf",
                            "file_data": f"data:{block['media_type']};base64,{block['data']}",
                        }
                    )
            converted.append({"role": role, "content": parts})
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "provider_state"
                and block.get("provider") == "openai_responses"
            ):
                converted.extend(cast(list[dict[str, Any]], block.get("items", [])))
        text = "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text:
            converted.append({"role": role, "content": text})
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                converted.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    }
                )
            elif block.get("type") == "tool_result":
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": str(block.get("content", "")),
                    }
                )
    return converted


class OpenAIResponsesProvider:
    def __init__(self, settings: Settings):
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("The openai package is required for OpenAI Responses use") from exc
        self._client = AsyncOpenAI(
            api_key=settings.require_api_key(), base_url=settings.base_url, max_retries=0,
            timeout=settings.model_timeout,
        )

    async def close(self) -> None:
        await self._client.close()

    def _parameters(self, request: ModelRequest) -> dict[str, Any]:
        # Strict backends distinguish an absent tools field from an empty array.
        params: dict[str, Any] = {
            "model": request.model,
            "instructions": request.system,
            "input": cast(Any, convert_response_input(request.messages)),
            "max_output_tokens": request.max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        params.update(wire_parameters("openai_responses", request.generation))
        if request.tools:
            params["tools"] = cast(Any, convert_response_tools(request.tools))
        return params

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await cast(Any, self._client).responses.create(**self._parameters(request))
        except Exception as exc:
            raise stream_error(exc, started=False) from exc
        return parse_response(response)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = False
        result: ModelResponse | None = None
        limit = StreamLimit()
        items: set[int] = set()
        try:
            raw = await cast(Any, self._client).responses.create(
                **self._parameters(request), stream=True,
            )
            started = True
            async with closing_stream(raw):
                async for event in raw:
                    limit.event()
                    kind = field(event, "type", "")
                    if kind == "response.output_text.delta":
                        text = limit.add(str(field(event, "delta", "") or ""))
                        if text:
                            yield ModelStreamEvent("text_delta", text=text)
                    elif kind in {
                        "response.function_call_arguments.delta",
                        "response.reasoning_text.delta",
                        "response.reasoning_summary_text.delta",
                    }:
                        limit.add(str(field(event, "delta", "") or ""))
                    elif kind == "response.output_item.added":
                        index = field(event, "output_index")
                        if type(index) is not int or index < 0 or index in items:
                            raise ProviderError("Invalid model stream output item index")
                        items.add(index)
                        if len(items) > MAX_STREAM_ITEMS:
                            raise ProviderError("Model stream exceeded the output item limit")
                        limit.check_object(field(event, "item"))
                    elif kind in {"response.completed", "response.incomplete"}:
                        response = field(event, "response")
                        if response is None:
                            raise ProviderError("Model stream omitted its terminal response")
                        limit.check_object(response)
                        result = parse_response(response)
                        break
                    elif kind in {"response.failed", "error"}:
                        raise ProviderError("Model response stream failed")
            if result is None:
                raise ProviderError("Model response stream ended without a terminal response")
        except Exception as exc:
            raise stream_error(exc, started=started) from exc
        yield ModelStreamEvent("response_completed", response=result)


def parse_response(response: Any) -> ModelResponse:
    """Normalize both complete and terminal streaming Responses objects."""
    calls: list[ToolCall] = []
    provider_items: list[dict[str, Any]] = []
    incomplete_call = False
    output = field(response, "output", []) or []
    if len(output) > MAX_STREAM_ITEMS:
        raise ProviderError("Model response exceeded the output item limit")
    for item in output:
        kind = field(item, "type", "")
        if kind == "reasoning":
            provider_items.append(to_dict(item))
        elif kind == "function_call":
            if field(item, "status") not in {None, "completed"}:
                incomplete_call = True
            if not field(item, "call_id") or not field(item, "name"):
                # Keep final usage reportable, but never manufacture an identity
                # for an incomplete tool call or let the Host dispatch it.
                incomplete_call = True
                continue
            arguments = parse_arguments(str(field(item, "arguments", "{}") or "{}"))
            if "_invalid_json" in arguments:
                incomplete_call = True
            calls.append(ToolCall(
                str(field(item, "call_id", "")), str(field(item, "name", "")),
                arguments,
            ))
    usage = normalize_provider_usage("openai_responses", field(response, "usage"))
    status = str(field(response, "status", "completed") or "completed")
    if status == "incomplete":
        status = str(field(field(response, "incomplete_details"), "reason", "") or "incomplete")
    elif incomplete_call:
        status = "incomplete"
    elif calls and status == "completed":
        status = "tool_use"
    text = field(response, "output_text")
    if text is None:
        text = "".join(
            str(field(part, "text", ""))
            for item in output if field(item, "type") == "message"
            for part in field(item, "content", [])
            if field(part, "type") == "output_text"
        )
    return ModelResponse(
        str(text or ""), tuple(calls), status,
        usage, tuple(provider_items),
    )
