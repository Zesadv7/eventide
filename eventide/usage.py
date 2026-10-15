"""Provider usage normalization and replayable, exclusive request accounting.

A detail counter is not an additional total. Unknown usage never becomes a
reported zero, and aggregate/terminal events are never billing evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_miss_input_tokens",
    "uncached_input_tokens",
    "reasoning_tokens",
)
_TOTAL_FIELDS = ("input_tokens", "output_tokens")


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def normalize_usage(usage: Any) -> dict[str, int]:
    """Filter public counters without coercion or inventing absent values."""
    return {
        key: number
        for key in TOKEN_FIELDS
        if (number := _count(_field(usage, key))) is not None
    }


def normalize_provider_usage(provider: str, usage: Any) -> dict[str, int]:
    """Normalize SDK objects and dictionary fixtures into explicit total/detail counters."""
    result: dict[str, int] = {}
    fields = {
        "openai_compatible": {
            "input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"
        },
        "openai_responses": {"input_tokens": "input_tokens", "output_tokens": "output_tokens"},
        "anthropic": {"input_tokens": "input_tokens", "output_tokens": "output_tokens"},
    }.get(provider)
    if fields is None:
        return normalize_usage(usage)
    for target, source in fields.items():
        if (number := _count(_field(usage, source))) is not None:
            result[target] = number
    if provider == "anthropic":
        # Anthropic's base input counter excludes both cache buckets. OpenAI's
        # total input includes cached tokens, so that provider must not add them.
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if (number := _count(_field(usage, key))) is not None:
                result[key] = number
        if "input_tokens" in result and any(
            key in result for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
        ):
            result["uncached_input_tokens"] = result["input_tokens"]
            result["input_tokens"] += sum(
                result.get(key, 0)
                for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
            )
    else:
        prefix = "prompt" if provider == "openai_compatible" else "input"
        details = _field(usage, f"{prefix}_tokens_details")
        cached = _count(_field(details, "cached_tokens"))
        # DeepSeek-compatible responses expose top-level cache hit/miss values.
        if cached is None:
            cached = _count(_field(usage, "prompt_cache_hit_tokens"))
        if cached is not None:
            result["cache_read_input_tokens"] = cached
        if (missed := _count(_field(usage, "prompt_cache_miss_tokens"))) is not None:
            result["cache_miss_input_tokens"] = missed
        output = "completion" if provider == "openai_compatible" else "output"
        reasoning = _count(_field(_field(usage, f"{output}_tokens_details"), "reasoning_tokens"))
        if reasoning is not None:
            result["reasoning_tokens"] = reasoning
    return result


def usage_snapshot(usage: Any, source: Any = None) -> dict[str, Any]:
    """Describe known counters while preserving reported, estimated and unknown provenance."""
    counters = normalize_usage(usage)
    provenance = (
        source if isinstance(source, str) and source in {"reported", "estimated", "unknown"}
        else None
    )
    if provenance is None:
        provenance = "reported" if counters else "unknown"
    if not counters:
        provenance = "unknown"
    return {
        "usage": counters,
        "source": provenance,
        "complete": provenance == "reported" and all(key in counters for key in _TOTAL_FIELDS),
    }


def build_usage_ledger(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Rebuild exclusive usage from unique attempt facts, with explicit legacy fallback.

    The caller chooses Runs/Sessions by supplying canonical events. Child totals
    and terminal summaries are ignored: supply child events to include children.
    Runs containing attempt facts never also count their legacy response events.
    In-flight attempts remain unknown after a restart; retries are separate rows.
    """
    canonical = [
        event
        for event in events
        if not event.get("partial") and isinstance(event.get("payload"), Mapping)
    ]
    new_runs = {
        event.get("run_id")
        for event in canonical
        if event.get("type") in {"model.attempt_started", "model.attempt_finished"}
    }
    attempts: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = {}
    legacy: dict[tuple[str, str], Mapping[str, Any]] = {}
    for event in canonical:
        kind, payload = event.get("type"), event["payload"]
        run = str(event.get("run_id", ""))
        if kind in {"model.attempt_started", "model.attempt_finished"}:
            attempt_id = payload.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                raise ValueError("Model attempt requires a non-empty attempt_id")
            record = attempts.setdefault((run, attempt_id), {})
            slot = "start" if kind == "model.attempt_started" else "finish"
            previous = record.get(slot)
            # Replayed copies may carry a different transport envelope, but
            # conflicting facts for one attempt must never silently change a bill.
            if previous is not None and dict(previous["payload"]) != dict(payload):
                raise ValueError("Conflicting model attempt facts")
            record.setdefault(slot, event)
        elif event.get("run_id") not in new_runs and kind in {
            "model.response", "context.summary_generated"
        }:
            event_id = str(event.get("event_id", ""))
            if not event_id:
                raise ValueError("Legacy usage requires an event_id")
            legacy.setdefault((run, event_id), event)
    calls = [_attempt_row(key, record) for key, record in attempts.items()]
    calls.extend(_legacy_row(event) for event in legacy.values())
    calls.sort(key=lambda row: (row["started_at"], row["run_id"], row["attempt_id"]))
    reported: dict[str, int] = {}
    estimated: dict[str, int] = {}
    for call in calls:
        target = reported if call["usage_source"] == "reported" else estimated
        if call["usage_source"] == "unknown":
            continue
        for key, count in call["usage"].items():
            target[key] = target.get(key, 0) + count
    return {
        "schema_version": 1,
        "source": "eventide.event_log",
        "scope": "exclusive",
        "calls": calls,
        "reported_totals": reported,
        "estimated_totals": estimated,
        "attempt_count": len(calls),
        "unknown_attempts": sum(call["usage_source"] == "unknown" for call in calls),
        "incomplete_attempts": sum(not call["usage_complete"] for call in calls),
        "complete": all(call["usage_complete"] for call in calls),
    }


def _attempt_row(
    key: tuple[str, str], record: dict[str, Mapping[str, Any]]
) -> dict[str, Any]:
    start = record.get("start")
    finish = record.get("finish")
    event = start or finish
    assert event is not None
    payload = {**(start["payload"] if start else {}), **(finish["payload"] if finish else {})}
    if start and finish:
        for field in ("request_id", "attempt"):
            if start["payload"].get(field) != finish["payload"].get(field):
                raise ValueError("Conflicting model attempt identity")
    snapshot = usage_snapshot(payload.get("usage") if finish else None, payload.get("usage_source"))
    row = _identity(event)
    row.update(
        {
            "request_id": payload.get("request_id"),
            "attempt_id": key[1],
            "attempt": _count(payload.get("attempt")),
            "kind": payload.get("kind", "agent"),
            "model": payload.get("model"),
            "status": payload.get("status", "unknown") if finish else "unknown",
            "request_observed": start is not None,
            "started_at": start["ts"] if start else event.get("ts", 0),
            "finished_at": finish.get("ts") if finish else None,
            "usage": snapshot["usage"],
            "usage_source": snapshot["source"],
            "usage_complete": snapshot["complete"],
        }
    )
    return row


def _legacy_row(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    snapshot = usage_snapshot(payload.get("usage"))
    return {
        **_identity(event),
        "request_id": None,
        "attempt_id": f"legacy:{event['event_id']}",
        "attempt": None,
        "kind": "context_summary" if event["type"] == "context.summary_generated" else "agent",
        "model": payload.get("model"),
        "status": "completed",
        "request_observed": False,
        "started_at": event.get("ts", 0),
        "finished_at": event.get("ts"),
        "usage": snapshot["usage"],
        "usage_source": snapshot["source"],
        # Historical calls lack evidence for hidden retries and missing requests.
        "usage_complete": False,
    }


def _identity(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: event.get(key)
        for key in ("workspace_id", "session_id", "run_id")
    }
