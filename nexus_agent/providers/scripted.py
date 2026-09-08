"""Deterministic provider used by offline evaluations and tests."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from nexus_agent.models import ModelRequest, ModelResponse, ToolCall
from nexus_agent.providers.base import ProviderError


class ScriptedProvider:
    def __init__(self, script: list[dict[str, Any]]):
        self.script = deque(script)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.script:
            return ModelResponse("Script completed.")
        item = self.script.popleft()
        if item.get("delay_ms"):
            await asyncio.sleep(float(item["delay_ms"]) / 1000)
        if "error" in item:
            raise ProviderError(str(item["error"]), retryable=bool(item.get("retryable")))
        calls = tuple(
            ToolCall(
                str(call.get("id", f"call_{index}")),
                str(call["name"]),
                dict(call.get("arguments", {})),
            )
            for index, call in enumerate(item.get("tool_calls", []), 1)
        )
        return ModelResponse(
            text=str(item.get("text", "")),
            tool_calls=calls,
            stop_reason=str(item.get("stop_reason") or ("tool_use" if calls else "end_turn")),
            usage={k: int(v) for k, v in item.get("usage", {}).items()},
        )
