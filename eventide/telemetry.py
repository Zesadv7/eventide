"""Privacy-preserving trace projections over the canonical Event Log.

No instrumentation is installed globally and importing this module requires no
OpenTelemetry packages. Intervals describe committed observations: tool spans
include approval waits, and unresolved outcomes stay unknown. Token counters are
reported usage, never estimates of billing for failed or retried requests.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from typing import Any

from eventide import __version__
from eventide.usage import TOKEN_FIELDS, build_usage_ledger

_TERMINALS = {"run.completed", "run.failed", "run.interrupted"}
_REASONS = {
    "cancelled",
    "step_budget",
    "task_step_budget",
    "token_budget",
    "admission_failed",
    "unknown_tool_outcome",
}
_TOOLS = {
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "compact",
    "read_tool_result",
    "todo_write",
    "load_skill",
    "search_knowledge",
    "delegate",
    "delegate_readonly",
    "review",
}
_PROVIDERS = {"anthropic", "openai_compatible", "openai_responses", "scripted"}


def _id(value: str, size: int) -> str:
    # Raw user-supplied Session, Run and call identifiers can contain secrets.
    return hashlib.sha256(value.encode()).hexdigest()[:size]


def _number(value: Any) -> int | float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return value
    return None


def _usage(payload: Mapping[str, Any]) -> dict[str, int | float]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return {}
    return {
        f"eventide.usage.{key}": number
        for key in ("input_tokens", "output_tokens")
        if (number := _number(usage.get(key))) is not None
    }


def _span(
    run_id: str,
    trace_id: str,
    operation: str,
    identity: str,
    start: float,
    end: float,
    status: str,
    parent_id: str | None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    end = max(start, end)
    return {
        "name": f"eventide.{operation}",
        "trace_id": trace_id,
        "span_id": _id(f"span:{run_id}:{operation}:{identity}", 16),
        "parent_span_id": parent_id,
        "start_time_ns": int(start * 1_000_000_000),
        "end_time_ns": int(end * 1_000_000_000),
        "status": status,
        "attributes": {
            "eventide.operation": operation,
            "eventide.status": status,
            "eventide.duration_ms": (end - start) * 1000,
            **(attributes or {}),
        },
        "links": [],
    }


def build_run_trace(
    events: Iterable[Mapping[str, Any]],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build deterministic JSON spans; select a Run and its known descendants.

    Pass canonical events, including related Sessions for worker traces. A
    continuation is a link, not a child. Missing continuation events are marked
    as unobserved; no missing Run execution or trace identity is fabricated. Fixed
    names and allowlisted metadata exclude prompts, model names, arbitrary tool
    names, parameters, results, paths, errors and credentials. Run counters count
    only that Run's model responses, excluding inclusive child usage totals.
    """
    runs: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        if event.get("partial") or not isinstance(event.get("run_id"), str):
            continue
        ts = _number(event.get("ts"))
        if ts is None or not isinstance(event.get("payload"), Mapping):
            raise ValueError("Trace requires canonical events with timestamps and payloads")
        identity = str(event["run_id"])
        runs.setdefault(identity, []).append(event)
    for items in runs.values():
        items.sort(key=lambda event: event.get("session_seq", 0))
        if len({event.get("session_id") for event in items}) != 1:
            raise ValueError("Run events have conflicting Session identities")
    if run_id is not None and run_id not in runs:
        raise ValueError("Run has no canonical events")

    intent: dict[str, dict[str, Any]] = {}
    parents: dict[str, str] = {}
    for identity, items in runs.items():
        intent[identity] = {}
        for event in items:
            if event["type"] in {"run.admitted", "run.started"}:
                intent[identity].update(event["payload"])
        parent = intent[identity].get("parent_run_id")
        if isinstance(parent, str):
            if identity in parents and parents[identity] != parent:
                raise ValueError("Worker has conflicting parent identities")
            parents[identity] = parent
        for event in items:
            if event["type"] == "worker.started":
                child = event["payload"].get("child_run_id")
                if isinstance(child, str):
                    if child in parents and parents[child] != identity:
                        raise ValueError("Worker has conflicting parent identities")
                    parents[child] = identity

    def root(identity: str) -> str:
        visited: set[str] = set()
        while identity in parents:
            if identity in visited:
                raise ValueError("Trace parent hierarchy contains a cycle")
            visited.add(identity)
            identity = parents[identity]
        return identity

    selected = {
        identity
        for identity in runs
        if run_id is None or identity == run_id or _is_descendant(identity, run_id, parents)
    }
    trace_ids = {identity: _id(f"trace:{root(identity)}", 32) for identity in runs}
    continuations = {
        identity: metadata["continuation_of"]
        for identity, metadata in intent.items()
        if isinstance(metadata.get("continuation_of"), str)
    }
    abandoned = _abandoned_tools(runs, continuations)
    spans: list[dict[str, Any]] = []
    for identity, items in runs.items():
        if identity not in selected:
            continue
        terminal = next((e for e in items if e["type"] in _TERMINALS), None)
        reason = terminal["payload"].get("reason") if terminal else None
        status = terminal["type"].removeprefix("run.") if terminal else "unknown"
        if status == "interrupted" and reason == "cancelled":
            status = "cancelled"
        end = float((terminal or items[-1])["ts"])
        metadata = intent[identity]
        attributes: dict[str, Any] = {"eventide.usage.scope": "exclusive"}
        provider = metadata.get("provider")
        attributes["eventide.provider"] = provider if provider in _PROVIDERS else "other"
        mode = metadata.get("mode")
        attributes["eventide.mode"] = mode if mode in {"auto", "plan", "agent"} else "unknown"
        role = metadata.get("worker_role")
        if role in {"researcher", "reviewer", "writer"}:
            attributes["eventide.worker.role"] = role
        if reason in _REASONS:
            attributes["eventide.reason"] = reason
        parent = parents.get(identity)
        parent_id = _id(f"span:{parent}:run:run", 16) if parent else None
        run_span = _span(
            identity,
            trace_ids[identity],
            "run",
            "run",
            float(items[0]["ts"]),
            end,
            status,
            parent_id,
            attributes,
        )
        continuation = metadata.get("continuation_of")
        if isinstance(continuation, str):
            run_span["attributes"]["eventide.continuation.observed"] = continuation in trace_ids
            if continuation in trace_ids:
                run_span["links"].append(
                    {
                        "trace_id": trace_ids[continuation],
                        "span_id": _id(f"span:{continuation}:run:run", 16),
                        "relation": "continuation",
                    }
                )
        operations = _operation_spans(identity, items, end, status, trace_ids[identity], abandoned)
        for key in TOKEN_FIELDS:
            counter = f"eventide.usage.{key}"
            known = [
                span["attributes"][counter]
                for span in operations
                if span["attributes"]["eventide.operation"] in {"model", "summary"}
                and counter in span["attributes"]
            ]
            if known:
                run_span["attributes"][counter] = sum(known)
        run_span["attributes"]["eventide.usage.complete"] = all(
            span["attributes"].get("eventide.usage.complete", False)
            for span in operations
            if span["attributes"]["eventide.operation"] in {"model", "summary"}
        )
        spans.extend([run_span, *operations])
    spans.sort(key=lambda span: (span["start_time_ns"], span["span_id"]))
    return {"schema_version": 1, "source": "eventide.event_log", "spans": spans}


def _is_descendant(identity: str, ancestor: str, parents: dict[str, str]) -> bool:
    seen: set[str] = set()
    while identity in parents:
        if identity in seen:
            raise ValueError("Trace parent hierarchy contains a cycle")
        seen.add(identity)
        identity = parents[identity]
        if identity == ancestor:
            return True
    return False


def _operation_spans(
    identity: str,
    events: list[Mapping[str, Any]],
    end: float,
    run_status: str,
    trace_id: str,
    abandoned: dict[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    parent_id = _id(f"span:{identity}:run:run", 16)
    spans: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    has_summaries = any(e["type"] == "context.summary_generated" for e in events)
    has_attempts = any(
        e["type"] in {"model.attempt_started", "model.attempt_finished"} for e in events
    )
    if has_attempts:
        for call in build_usage_ledger(events)["calls"]:
            operation = "summary" if call["kind"] == "context_summary" else "model"
            status = call["status"]
            status = status if status in {"completed", "failed", "cancelled"} else "unknown"
            attempt_attributes = {
                "eventide.usage.complete": call["usage_complete"],
                "eventide.usage.source": call["usage_source"],
                "eventide.request_observed": call["request_observed"],
            }
            if call["usage_source"] == "reported":
                attempt_attributes.update(
                    {f"eventide.usage.{key}": count for key, count in call["usage"].items()}
                )
            spans.append(
                _span(
                    identity, trace_id, operation, call["attempt_id"],
                    float(call["started_at"]),
                    float(call["finished_at"] if call["finished_at"] is not None else end),
                    status, parent_id, attempt_attributes,
                )
            )
    for event in events:
        kind, payload, timestamp = event["type"], event["payload"], float(event["ts"])
        operation = "summary" if kind.startswith("context.summary_") else "model"
        if has_attempts and kind in {
            "model.request", "model.retry", "model.response", "context.summary_requested",
            "context.summary_generated", "context.compacted",
        }:
            continue
        if kind in {"model.request", "context.summary_requested"}:
            if operation in pending:
                previous = pending.pop(operation)
                _finish_span(previous, timestamp, "unknown")
                spans.append(previous)
            attributes: dict[str, Any] = {"eventide.usage.complete": False}
            for key in ("step", "request_chars", "tool_count"):
                if (number := _number(payload.get(key))) is not None:
                    attributes[f"eventide.{key}"] = number
            pending[operation] = _span(
                identity,
                trace_id,
                operation,
                str(event["event_id"]),
                timestamp,
                end,
                "unknown",
                parent_id,
                attributes,
            )
        elif kind == "model.retry":
            operation = "summary" if payload.get("kind") == "context_summary" else "model"
            if operation in pending:
                attributes = pending[operation]["attributes"]
                attributes["eventide.retry_events"] = attributes.get("eventide.retry_events", 0) + 1
        elif kind in {"model.response", "context.summary_generated", "context.compacted"}:
            if kind == "context.compacted":
                if has_summaries or not _usage(payload):
                    continue
                operation = "summary"
            current = pending.pop(operation, None)
            # Historical summaries have no request timestamp: a zero-width observation.
            if current is None:
                current = _span(
                    identity,
                    trace_id,
                    operation,
                    str(event["event_id"]),
                    timestamp,
                    timestamp,
                    "unknown",
                    parent_id,
                )
                current["attributes"]["eventide.request_observed"] = False
            _finish_span(current, timestamp, "completed")
            usage = _usage(payload)
            current["attributes"].update(usage)
            current["attributes"]["eventide.usage.complete"] = len(usage) == 2 and not current[
                "attributes"
            ].get("eventide.retry_events")
            spans.append(current)
        elif kind == "tool.prepared":
            call_id = str(payload["call_id"])
            name = payload.get("name", "")
            category = (
                name if name in _TOOLS else "mcp" if str(name).startswith("mcp__") else "custom"
            )
            current = _span(
                identity,
                trace_id,
                "tool",
                call_id,
                timestamp,
                end,
                "unknown",
                parent_id,
                {"eventide.tool": category, "eventide.readonly": payload.get("readonly") is True},
            )
            tools[call_id] = current
            spans.append(current)
        elif kind == "tool.completed" and str(payload.get("call_id")) in tools:
            current = tools.pop(str(payload["call_id"]))
            _finish_span(current, timestamp, "failed" if payload.get("is_error") else "completed")
            metadata = payload.get("metadata", {})
            for key in ("duration_ms", "stdout_bytes", "stderr_bytes"):
                if (number := _number(metadata.get(key))) is not None:
                    current["attributes"][f"eventide.command.{key}"] = number
            for key in ("timed_out", "truncated"):
                if isinstance(metadata.get(key), bool):
                    current["attributes"][f"eventide.command.{key}"] = metadata[key]
    for call_id, current in tools.items():
        if resolution := abandoned.get((identity, call_id)):
            _finish_span(current, float(resolution["ts"]), "abandoned")
    for current in pending.values():
        status = run_status if run_status in {"failed", "cancelled"} else "unknown"
        _finish_span(current, end, status)
        spans.append(current)
    return spans


def _abandoned_tools(
    runs: dict[str, list[Mapping[str, Any]]],
    continuations: dict[str, str],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Match recovery facts to the newest call within the same Session lineage."""
    sessions: dict[str, list[Mapping[str, Any]]] = {}
    for items in runs.values():
        sessions.setdefault(str(items[0]["session_id"]), []).extend(items)
    resolved: dict[tuple[str, str], Mapping[str, Any]] = {}
    for events in sessions.values():
        active: dict[str, str] = {}
        for event in sorted(events, key=lambda item: item.get("session_seq", 0)):
            payload, identity = event["payload"], str(event["run_id"])
            if event["type"] == "model.response":
                content = payload.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, Mapping) and block.get("type") == "tool_use":
                            active[str(block["id"])] = identity
            elif event["type"] == "tool.prepared":
                active[str(payload["call_id"])] = identity
            elif event["type"] in {"tool.completed", "tool.abandoned"}:
                call_id = str(payload["call_id"])
                source = active.get(call_id)
                if source == identity or source and _is_descendant(identity, source, continuations):
                    if event["type"] == "tool.abandoned":
                        resolved[(source, call_id)] = event
                    active.pop(call_id)
    return resolved


def _finish_span(span: dict[str, Any], timestamp: float, status: str) -> None:
    span["end_time_ns"] = max(span["start_time_ns"], int(timestamp * 1_000_000_000))
    span["status"] = status
    span["attributes"]["eventide.status"] = status
    span["attributes"]["eventide.duration_ms"] = (
        span["end_time_ns"] - span["start_time_ns"]
    ) / 1_000_000


def export_run_trace(
    events: Iterable[Mapping[str, Any]],
    *,
    exporter: Any,
    run_id: str | None = None,
) -> int:
    """Explicitly export through a caller-owned OTel SpanExporter; return count.

    Requires the optional opentelemetry-sdk package only at invocation. This
    creates a local tracing pipeline, never replaces the global provider, and
    never selects a network exporter. The caller owns configuration and shutdown
    of the injected exporter. Export failure propagates without changing events.
    """
    projection = build_run_trace(events, run_id=run_id)
    try:
        from opentelemetry.context import Context
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
        from opentelemetry.sdk.trace.export import SpanExportResult
        from opentelemetry.sdk.trace.id_generator import IdGenerator
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        from opentelemetry.trace import (
            Link,
            NonRecordingSpan,
            SpanContext,
            SpanKind,
            Status,
            StatusCode,
            TraceFlags,
            set_span_in_context,
        )
    except ImportError as exc:
        raise RuntimeError("OTel export requires the optional opentelemetry-sdk package") from exc

    class ReplayIds(IdGenerator):
        span_id = 1
        trace_id = 1

        def generate_span_id(self) -> int:
            return self.span_id

        def generate_trace_id(self) -> int:
            return self.trace_id

    class Collector(SpanProcessor):
        def __init__(self) -> None:
            self.spans: list[ReadableSpan] = []

        def on_end(self, span: ReadableSpan) -> None:
            self.spans.append(span)

    ids, collector = ReplayIds(), Collector()
    provider = TracerProvider(
        id_generator=ids,
        sampler=ALWAYS_ON,
        resource=Resource({"service.name": "eventide"}),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(collector)
    tracer = provider.get_tracer("eventide.telemetry", __version__)

    def context(trace_id: str, span_id: str) -> Any:
        return SpanContext(
            int(trace_id, 16),
            int(span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )

    try:
        for projected in projection["spans"]:
            ids.span_id = int(projected["span_id"], 16)
            ids.trace_id = int(projected["trace_id"], 16)
            parent = projected["parent_span_id"]
            parent_context = (
                set_span_in_context(NonRecordingSpan(context(projected["trace_id"], parent)))
                if parent
                else Context()
            )
            links = [
                Link(
                    context(link["trace_id"], link["span_id"]),
                    {
                        "eventide.relation": link["relation"],
                    },
                )
                for link in projected["links"]
            ]
            span = tracer.start_span(
                projected["name"],
                context=parent_context,
                links=links,
                attributes=projected["attributes"],
                start_time=projected["start_time_ns"],
                kind=SpanKind.CLIENT
                if projected["name"]
                in {
                    "eventide.model",
                    "eventide.summary",
                }
                else SpanKind.INTERNAL,
            )
            status = projected["status"]
            span.set_status(
                Status(
                    StatusCode.ERROR
                    if status == "failed"
                    else StatusCode.OK
                    if status == "completed"
                    else StatusCode.UNSET,
                )
            )
            span.end(end_time=projected["end_time_ns"])
        if collector.spans and exporter.export(tuple(collector.spans)) != SpanExportResult.SUCCESS:
            raise RuntimeError("OpenTelemetry exporter rejected the trace")
        return len(collector.spans)
    finally:
        provider.shutdown()
