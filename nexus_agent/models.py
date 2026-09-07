"""Provider-neutral runtime data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def as_block(self) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.arguments}


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    model: str
    max_tokens: int = 8_000


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)
    provider_items: tuple[dict[str, Any], ...] = ()

    def content_blocks(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if self.provider_items:
            blocks.append(
                {
                    "type": "provider_state",
                    "provider": "openai_responses",
                    "items": list(self.provider_items),
                }
            )
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        blocks.extend(call.as_block() for call in self.tool_calls)
        return blocks


@dataclass(frozen=True, slots=True)
class RunRequest:
    prompt: str
    session_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    session_id: str
    status: str
    output: str
    steps: int
    tool_calls: int
    duration_ms: float
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False

    def as_block(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": self.call_id,
            "content": self.content,
            "is_error": self.is_error,
        }
