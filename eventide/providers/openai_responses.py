"""OpenAI Responses API adapter."""

from __future__ import annotations

import json
from typing import Any, cast

from eventide.config import Settings
from eventide.models import ModelRequest, ModelResponse, ToolCall
from eventide.providers.base import ProviderError


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
        self._client = AsyncOpenAI(api_key=settings.require_api_key(), base_url=settings.base_url)

    async def close(self) -> None:
        await self._client.close()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            # Omit the `tools` field when empty; the Responses API treats an
            # empty list differently from an absent field on strict backends.
            params: dict[str, Any] = {
                "model": request.model,
                "instructions": request.system,
                "input": cast(Any, convert_response_input(request.messages)),
                "max_output_tokens": request.max_tokens,
                "store": False,
                "include": ["reasoning.encrypted_content"],
            }
            if request.tools:
                params["tools"] = cast(Any, convert_response_tools(request.tools))
            response = await cast(Any, self._client).responses.create(**params)
        except Exception as exc:
            text = str(exc).lower()
            retryable = any(token in text for token in ("429", "500", "502", "503", "529"))
            raise ProviderError(str(exc), retryable=retryable) from exc

        calls: list[ToolCall] = []
        provider_items: list[dict[str, Any]] = []
        for item in getattr(response, "output", []) or []:
            raw: Any = item
            if getattr(raw, "type", "") == "reasoning":
                if hasattr(raw, "model_dump"):
                    provider_items.append(raw.model_dump(exclude_none=True))
                elif isinstance(raw, dict):
                    provider_items.append(dict(raw))
                continue
            if getattr(raw, "type", "") != "function_call":
                continue
            arguments_text = getattr(raw, "arguments", "{}") or "{}"
            try:
                arguments = json.loads(arguments_text)
            except json.JSONDecodeError:
                arguments = {"_invalid_json": arguments_text}
            calls.append(ToolCall(raw.call_id, raw.name, arguments))
        usage_obj: Any = getattr(response, "usage", None)
        usage = {
            "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
        }
        status = str(getattr(response, "status", "completed") or "completed")
        if status == "incomplete":
            # Preserve the reason (e.g. max_output_tokens) so truncation is detectable.
            details = getattr(response, "incomplete_details", None)
            status = str(getattr(details, "reason", "") or "") or "incomplete"
        return ModelResponse(
            str(getattr(response, "output_text", "") or ""),
            tuple(calls),
            "tool_use" if calls else status,
            usage,
            tuple(provider_items),
        )
