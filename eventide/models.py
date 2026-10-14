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
    mode: str = "auto"
    attachment_ids: list[str] = field(default_factory=list)
    model: str | None = None


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
    turn_id: str | None = None
    continuation_of: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceTarget:
    path: str | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    name: str
    path: str
    git_root: str | None
    created_at: float


@dataclass(frozen=True, slots=True)
class ContinuationRequest:
    session_id: str


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    session_id: str
    session_seq: int
    turn_id: str | None
    run_id: str | None
    ts: float
    type: str
    role: str
    author: str
    payload: dict[str, Any]
    partial: bool = False
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a command, with bounded output and explicit process status."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: float = 0.0
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_omitted_bytes: int = 0
    stderr_omitted_bytes: int = 0

    @property
    def truncated(self) -> bool:
        return self.stdout_omitted_bytes > 0 or self.stderr_omitted_bytes > 0

    @property
    def is_error(self) -> bool:
        return self.timed_out or self.exit_code != 0

    def render(self) -> str:
        if self.timed_out:
            status = "Command timed out"
        else:
            status = "Command failed" if self.is_error else "Command finished"
        parts = [f"{status} (exit_code={self.exit_code})."]
        if self.stdout:
            parts.append(f"stdout:\n{self.stdout}")
        if self.stderr:
            parts.append(f"stderr:\n{self.stderr}")
        if not self.stdout and not self.stderr:
            parts.append("(no output)")
        return "\n".join(parts)

    def metadata(self) -> dict[str, Any]:
        """Process facts; captured text already appears in the tool content."""
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_omitted_bytes": self.stdout_omitted_bytes,
            "stderr_omitted_bytes": self.stderr_omitted_bytes,
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_block(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": self.call_id,
            "content": self.content,
            "is_error": self.is_error,
        }
