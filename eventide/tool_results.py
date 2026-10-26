"""Lossless Event Log lookup and bounded model-facing tool-result pages."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eventide.acceptance_output import AcceptanceOutputs
from eventide.command_logs import CommandLogs
from eventide.store import RuntimeStore

INLINE_RESULT_CHARS = 12_000
PAGE_DEFAULT_CHARS = 4_000
PAGE_MAX_CHARS = 12_000
PREVIEW_HEAD_CHARS = 8_000
PREVIEW_TAIL_CHARS = 2_000


TOOL = {
    "name": "read_tool_result",
    "description": "Page results or recorded command/acceptance stdout/stderr.",
    "input_schema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "call_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX_CHARS},
            "stream": {"type": "string", "enum": ["stdout", "stderr"]},
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


def reader(
    store: RuntimeStore, session_id: str, *, acceptance: dict[str, Any] | None = None,
    current_run_id: str | None = None,
    outputs: AcceptanceOutputs | None = None,
    command_logs: CommandLogs | None = None,
) -> Callable[..., str]:
    """Session lookup plus a controller-frozen allowlist of prior acceptance calls."""

    def read_tool_result(
        run_id: str,
        call_id: str,
        offset: int = 0,
        limit: int = PAGE_DEFAULT_CHARS,
        stream: str | None = None,
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
        if stream not in (None, "stdout", "stderr"):
            return "Error: stream must be stdout or stderr, or omitted for legacy content"
        result = store.get_tool_result(session_id, run_id, call_id)
        evidence = store.get_acceptance_output(session_id, run_id, call_id)
        command = (command_logs.recorded(session_id, run_id, call_id)
                   if command_logs is not None else None)
        origin = ""
        if (
            result is None and evidence is None and acceptance is not None
            and current_run_id == acceptance["reader_run_id"]
            and run_id == acceptance["run_id"] and call_id in acceptance["call_ids"]
        ):
            result = store.get_tool_result(acceptance["session_id"], run_id, call_id)
            evidence = store.get_acceptance_output(acceptance["session_id"], run_id, call_id)
            origin = (
                f"[Acceptance evidence: delivery={acceptance['delivery_id']}; "
                f"round={acceptance['round']}; patch={acceptance['patch_sha256']}]\n"
            )
        if result is None and evidence is None and command is None:
            return "Error: tool result not found in this session"
        if stream is not None and command is not None:
            assert command_logs is not None
            page = command_logs.page(run_id, call_id, stream, offset, limit)
            if not page["available"]:
                return f"Error: recorded command log unavailable: {page['availability']}"
            return (
                f"[Command {stream} {page['start']}:{page['end']} "
                f"of {page['total_chars']} characters; "
                f"capture={page['capture_state']}; reason={page['reason']}; "
                f"next_offset={page['next_offset']}]\n{page['text']}"
            )
        if stream is not None:
            if evidence is None:
                return "Error: this result has no separate stdout/stderr evidence"
            archive = outputs or AcceptanceOutputs(store, store.path.parent)
            return origin + archive.page(run_id, evidence, stream, offset, limit)
        if result is None:
            return "Error: no completed tool content; use a recorded command or acceptance stream"
        content = result["content"]
        total = len(content)
        start = min(offset, total)
        end = min(start + limit, total)
        next_hint = f"; next offset {end}" if end < total else "; end of result"
        return origin + f"[Tool result {start}:{end} of {total}{next_hint}]\n{content[start:end]}"

    return read_tool_result
