"""A bounded failure window projected from completed tools and source evidence."""

from __future__ import annotations

from typing import Any

from eventide.models import ToolCall, ToolResult
from eventide.store import RuntimeStore
from eventide.workspace import digest

FAILURE_WINDOW = 3
_RESULTS = {"tool.completed", "tool.outcome_unknown"}


def _failure_evidence(metadata: dict[str, Any]) -> bool:
    """Legacy Error:-prefixed strings are ambiguous, especially file contents."""
    code = metadata.get("code")
    exit_code = metadata.get("exit_code")
    return bool(
        not metadata.get("outcome_unknown")
        and (
            code == "timeout"
            or code == "execution_failed"
            and (
                type(exit_code) is int
                and exit_code != 0
                or isinstance(metadata.get("exception_type"), str)
                and metadata["exception_type"]
            )
        )
    )


def failure_fingerprint(call: ToolCall, result: ToolResult) -> str | None:
    """Hash actual inputs/outcomes before redaction; never merge different secrets."""
    if not result.is_error or not _failure_evidence(result.metadata):
        return None
    return digest(
        {
            "name": call.name,
            "arguments": call.arguments,
            "content": result.content,
            "code": result.metadata["code"],
            "exit_code": result.metadata.get("exit_code"),
            "timed_out": result.metadata.get("timed_out"),
            "exception_type": result.metadata.get("exception_type"),
        }
    )


def _history(store: RuntimeStore, run_id: str) -> list[dict[str, Any]]:
    """Only explicit Continue inherits a window; an ordinary new Run starts fresh."""
    chunks: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    count = 0
    while run_id and run_id not in seen:
        seen.add(run_id)
        identity = store.get_run_identity(run_id)
        if identity is None:
            return []
        events = store.run_events(run_id)
        chunks.append(events)
        count += sum(event["type"] in _RESULTS and not event["partial"] for event in events)
        if count >= FAILURE_WINDOW:
            break
        run_id = identity.get("continuation_of") or ""
    return [event for chunk in reversed(chunks) for event in chunk]


def failure_loop_candidate(store: RuntimeStore, run_id: str) -> dict[str, Any] | None:
    """Require the last three actual outcomes and verifiable equal checkpoints.

    A post-tool checkpoint updates that outcome until the next tool is prepared.
    Read-only tools initially reuse the last checkpoint; the owner must refresh it
    once before accepting a candidate. No counter or cache is a second fact source.
    """
    checkpoint: dict[str, Any] | None = None
    window: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    for event in _history(store, run_id):
        if event["partial"]:
            continue
        payload, kind = event["payload"], event["type"]
        if kind == "workspace.checkpoint":
            value = payload.get("checkpoint")
            checkpoint = (
                value
                if isinstance(value, dict) and value and value.get("available") is not False
                else None
            )
            if pending is not None:
                pending["checkpoint"] = checkpoint
        elif kind == "tool.prepared":
            pending = None
        elif kind in _RESULTS:
            metadata = payload.get("metadata", {})
            signature = (
                payload.get("failure_fingerprint")
                if kind == "tool.completed"
                and payload.get("is_error") is True
                and _failure_evidence(metadata)
                else None
            )
            pending = {
                "run_id": event["run_id"],
                "call_id": payload.get("call_id"),
                "name": payload.get("name"),
                "signature": signature,
                "checkpoint": checkpoint,
            }
            window.append(pending)
            window = window[-FAILURE_WINDOW:]
    if len(window) != FAILURE_WINDOW:
        return None
    last = window[-1]
    if (
        not last["signature"]
        or last["checkpoint"] is None
        or any(
            item["signature"] != last["signature"] or item["checkpoint"] != last["checkpoint"]
            for item in window
        )
    ):
        return None
    return {
        "threshold": FAILURE_WINDOW,
        "matching_failure_window": len(window),
        "tool": last["name"],
        "failure_fingerprint": last["signature"],
        "checkpoint_hash": digest(last["checkpoint"]),
        "continued_failure_chain": any(item["run_id"] != run_id for item in window),
        "calls": [{"run_id": item["run_id"], "call_id": item["call_id"]} for item in window],
    }
