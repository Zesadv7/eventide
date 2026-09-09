"""Lossless Event Log lookup and bounded model-facing tool-result pages."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eventide.store import RuntimeStore

INLINE_RESULT_CHARS = 12_000
PAGE_DEFAULT_CHARS = 4_000
PAGE_MAX_CHARS = 12_000
PREVIEW_HEAD_CHARS = 8_000
PREVIEW_TAIL_CHARS = 2_000


TOOL = {
    "name": "read_tool_result",
    "description": "Read part of a complete tool result from this session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "call_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX_CHARS},
        },
        "required": ["run_id", "call_id"],
    },
}


def preview(event: dict[str, Any]) -> tuple[str, int]:
    """Return a bounded content projection and the omitted character count."""
    payload = event["payload"]
    content = payload.get("content", "")
    if not isinstance(content, str) or len(content) <= INLINE_RESULT_CHARS:
        return str(content), 0
    run_id = event.get("run_id") or ""
    call_id = payload.get("call_id", "")
    omitted = len(content) - PREVIEW_HEAD_CHARS - PREVIEW_TAIL_CHARS
    marker = (
        f"\n\n[Tool output preview: {len(content)} characters total; {omitted} omitted. "
        "Use read_tool_result "
        f'with run_id="{run_id}", call_id="{call_id}", offset and limit to read any page.]\n\n'
    )
    return content[:PREVIEW_HEAD_CHARS] + marker + content[-PREVIEW_TAIL_CHARS:], omitted


def reader(store: RuntimeStore, session_id: str) -> Callable[..., str]:
    """Build a session-scoped handler for the dynamic read_tool_result tool."""

    def read_tool_result(
        run_id: str,
        call_id: str,
        offset: int = 0,
        limit: int = PAGE_DEFAULT_CHARS,
    ) -> str:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return "Error: offset must be a non-negative integer"
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > PAGE_MAX_CHARS
        ):
            return f"Error: limit must be between 1 and {PAGE_MAX_CHARS}"
        result = store.get_tool_result(session_id, run_id, call_id)
        if result is None:
            return "Error: tool result not found in this session"
        content = result["content"]
        total = len(content)
        start = min(offset, total)
        end = min(start + limit, total)
        next_hint = f"; next offset {end}" if end < total else "; end of result"
        return f"[Tool result {start}:{end} of {total}{next_hint}]\n{content[start:end]}"

    return read_tool_result
