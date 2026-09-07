"""OpenAI-compatible Chat Completions adapter for domestic providers."""

from __future__ import annotations

import json
from typing import Any, cast

from nexus_agent.config import Settings
from nexus_agent.models import ModelRequest, ModelResponse, ToolCall
from nexus_agent.providers.base import ProviderError


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
        if role == "assistant":
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [b for b in content if b.get("type") == "tool_use"]
            item: dict[str, Any] = {"role": "assistant", "content": text or None}
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
        self._client = AsyncOpenAI(api_key=settings.require_api_key(), base_url=settings.base_url)

    async def close(self) -> None:
        await self._client.close()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await self._client.chat.completions.create(
                model=request.model,
                messages=cast(Any, convert_messages(request.messages, request.system)),
                tools=cast(Any, convert_tools(request.tools)),
                max_tokens=request.max_tokens,
            )
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
        usage = {
            "input_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
        }
        return ModelResponse(
            message.content or "", tuple(calls), choice.finish_reason or "stop", usage
        )
