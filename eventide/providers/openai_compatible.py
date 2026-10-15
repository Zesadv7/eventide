"""OpenAI-compatible Chat Completions adapter for domestic providers."""

from __future__ import annotations

import json
from typing import Any, cast

from eventide.config import Settings
from eventide.generation import wire_parameters
from eventide.models import ModelRequest, ModelResponse, ToolCall
from eventide.providers.base import ProviderError
from eventide.usage import normalize_provider_usage


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": item["name"],
                "description": item.get("description", ""),
                "parameters": item.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for item in tools
    ]


def convert_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message["role"], message.get("content", "")
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
                    parts.append({"type": "text", "text": str(block.get("text", ""))})
                elif block.get("type") == "image":
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{block['media_type']};base64,{block['data']}"
                            },
                        }
                    )
                elif block.get("type") == "file":
                    parts.append(
                        {
                            "type": "text",
                            "text": f"[Earlier file attachment: {block.get('name', 'file')}]",
                        }
                    )
            converted.append({"role": role, "content": parts})
            continue
        if role == "assistant":
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [b for b in content if b.get("type") == "tool_use"]
            item: dict[str, Any] = {"role": "assistant", "content": text or None}
            for block in content:
                if (
                    block.get("type") == "provider_state"
                    and block.get("provider") == "openai_compatible"
                ):
                    for state in block.get("items", []):
                        if isinstance(state.get("reasoning_content"), str):
                            item["reasoning_content"] = state["reasoning_content"]
            if calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("input", {})),
                        },
                    }
                    for call in calls
                ]
            converted.append(item)
            continue
        tool_results = [b for b in content if b.get("type") == "tool_result"]
        if tool_results:
            converted.extend(
                {
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": str(block.get("content", "")),
                }
                for block in tool_results
            )
        else:
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            converted.append({"role": role, "content": text})
    return converted


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings):
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("The openai package is required for OpenAI-compatible use") from exc
        self._client = AsyncOpenAI(
            api_key=settings.require_api_key(), base_url=settings.base_url, max_retries=0,
        )

    async def close(self) -> None:
        await self._client.close()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            # Strict OpenAI-compatible backends (e.g. DashScope) reject an empty
            # `tools` array; omit the field entirely when there are no tools.
            params: dict[str, Any] = {
                "model": request.model,
                "messages": cast(Any, convert_messages(request.messages, request.system)),
                "max_tokens": request.max_tokens,
            }
            params.update(wire_parameters("openai_compatible", request.generation))
            if request.tools:
                params["tools"] = cast(Any, convert_tools(request.tools))
            response = await cast(Any, self._client).chat.completions.create(**params)
        except Exception as exc:
            text = str(exc).lower()
            retryable = any(token in text for token in ("429", "500", "502", "503", "529"))
            raise ProviderError(str(exc), retryable=retryable) from exc
        choice = response.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            raw_call: Any = call
            try:
                arguments = json.loads(raw_call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_invalid_json": raw_call.function.arguments}
            calls.append(ToolCall(raw_call.id, raw_call.function.name, arguments))
        usage_obj = getattr(response, "usage", None)
        usage = normalize_provider_usage("openai_compatible", usage_obj)
        reasoning = getattr(message, "reasoning_content", None)
        return ModelResponse(
            message.content or "", tuple(calls), choice.finish_reason or "stop", usage,
            ({"reasoning_content": reasoning},) if isinstance(reasoning, str) else (),
            "openai_compatible",
        )
