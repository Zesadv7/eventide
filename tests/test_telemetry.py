"""Trace export derives evidence without emitting user or tool content."""

import builtins
import copy
import importlib
import json

import pytest

from eventide import telemetry
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.telemetry import build_run_trace, export_run_trace
from tests.test_runtime import settings_for


def event(kind, seq, payload=None, *, run="run_a", session="session_a", ts=None):
    return {
        "event_id": f"{session}:{seq}",
        "session_id": session,
        "run_id": run,
        "session_seq": seq,
        "type": kind,
        "ts": float(seq if ts is None else ts),
        "payload": payload or {},
        "partial": False,
    }


def spans(trace, name):
    return [span for span in trace["spans"] if span["name"] == f"eventide.{name}"]


def completed_events():
    return [
        event("run.started", 1, {"mode": "plan", "provider": "openai_compatible"}),
        event("model.request", 2, {"step": 1, "request_chars": 50}),
        event("model.response", 3, {"usage": {"input_tokens": 4, "output_tokens": 2}}),
        event("tool.prepared", 4, {"call_id": "call_a", "name": "read_file", "readonly": True}),
        event("tool.completed", 5, {"call_id": "call_a", "is_error": False}),
        event("run.completed", 6, {"usage": {"input_tokens": 4, "output_tokens": 2}}),
    ]


def test_projection_pairs_model_and_tool_and_is_deterministic():
    events = completed_events()
    before = copy.deepcopy(events)
    trace = build_run_trace(events)
    assert trace == build_run_trace(reversed(events))
    assert events == before
    (run,) = spans(trace, "run")
    (model,) = spans(trace, "model")
    (tool,) = spans(trace, "tool")
    assert {span["status"] for span in trace["spans"]} == {"completed"}
    assert model["parent_span_id"] == tool["parent_span_id"] == run["span_id"]
    assert model["attributes"]["eventide.usage.input_tokens"] == 4
    assert tool["attributes"]["eventide.duration_ms"] == 1000
    assert run["attributes"]["eventide.mode"] == "plan"
    assert run["attributes"]["eventide.provider"] == "openai_compatible"


async def test_real_host_log_exports_the_same_observed_usage_without_content(isolated_workspace):
    private = "DO_NOT_EXPORT_workspace_content"
    (isolated_workspace / "note.txt").write_text(private)
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "read",
                        "name": "read_file",
                        "arguments": {
                            "path": "note.txt",
                        },
                    }
                ],
                "usage": {"input_tokens": 6, "output_tokens": 2},
            },
            {"text": private, "usage": {"input_tokens": 8, "output_tokens": 3}},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest(private))
        events = host.store.run_events(result.run_id)
        assert result.status == "completed"
        trace = build_run_trace(events, run_id=result.run_id)
        (run,) = spans(trace, "run")
        assert run["attributes"]["eventide.usage.input_tokens"] == result.usage["input_tokens"]
        assert run["attributes"]["eventide.usage.output_tokens"] == result.usage["output_tokens"]
        (tool,) = spans(trace, "tool")
        assert tool["status"] == "completed"
        assert private not in json.dumps(trace)
        assert host.store.run_events(result.run_id) == events
    finally:
        await host.close()


def test_projection_allowlists_metadata_even_when_identifiers_contain_secrets():
    secret = "DO_NOT_EXPORT_private_api_key"
    events = [
        event(
            "run.started",
            1,
            {
                "provider": secret,
                "model": secret,
                "mode": secret,
                "api_key": secret,
                "workspace_id": secret,
            },
            run=secret,
            session=secret,
        ),
        event("message.user", 2, {"message": {"content": secret}}, run=secret, session=secret),
        event(
            "model.request",
            3,
            {"model": secret, "context_hash": secret},
            run=secret,
            session=secret,
        ),
        event(
            "model.response",
            4,
            {
                "text": secret,
                "message": {"content": secret},
                "usage": {"input_tokens": 0, "output_tokens": 0, secret: secret},
            },
            run=secret,
            session=secret,
        ),
        event(
            "tool.prepared",
            5,
            {
                "call_id": secret,
                "name": f"mcp__{secret}__{secret}",
                "arguments": {"password": secret},
            },
            run=secret,
            session=secret,
        ),
        event(
            "tool.completed",
            6,
            {
                "call_id": secret,
                "content": secret,
                "metadata": {
                    "stdout": secret,
                    "cwd": secret,
                    "duration_ms": 25,
                    "timed_out": False,
                    "truncated": True,
                    "stdout_bytes": 10,
                    "stderr_bytes": 2,
                },
            },
            run=secret,
            session=secret,
        ),
        event("run.failed", 7, {"error": secret, "reason": secret}, run=secret, session=secret),
    ]
    trace = build_run_trace(events)
    assert secret not in json.dumps(trace)
    (tool,) = spans(trace, "tool")
    assert tool["attributes"]["eventide.tool"] == "mcp"
    assert tool["attributes"]["eventide.command.duration_ms"] == 25
    assert tool["attributes"]["eventide.command.truncated"] is True


@pytest.mark.parametrize(
    "terminal,reason,status",
    [
        ("run.failed", None, "failed"),
        ("run.interrupted", "cancelled", "cancelled"),
        ("run.interrupted", "step_budget", "interrupted"),
        (None, None, "unknown"),
    ],
)
def test_unresolved_model_and_tool_outcomes_are_honest(terminal, reason, status):
    events = [
        event("run.admitted", 1),
        event("model.request", 2),
        event("tool.prepared", 3, {"call_id": "x", "name": "custom"}),
    ]
    if terminal:
        events.append(event(terminal, 4, {"reason": reason}))
    trace = build_run_trace(events)
    (run,) = spans(trace, "run")
    (tool,) = spans(trace, "tool")
    (model,) = spans(trace, "model")
    assert run["status"] == status
    assert model["status"] == (status if status in {"failed", "cancelled"} else "unknown")
    assert model["attributes"]["eventide.usage.complete"] is False
    assert tool["status"] == "unknown"


def test_summary_usage_is_counted_once_and_retry_billing_stays_unknown():
    events = [
        event("run.started", 1),
        event("context.summary_requested", 2, {"model": "private-model"}),
        event("model.retry", 3, {"kind": "context_summary", "error": "private-error"}),
        event(
            "context.summary_generated",
            4,
            {
                "text": "private-summary",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
        ),
        event("context.compacted", 5, {"usage": {"input_tokens": 5, "output_tokens": 7}}),
        event("model.request", 6),
        event("model.response", 7, {"usage": {"input_tokens": 2, "output_tokens": 3}}),
        event("run.completed", 8),
    ]
    trace = build_run_trace(events)
    (summary,) = spans(trace, "summary")
    (run,) = spans(trace, "run")
    assert summary["attributes"]["eventide.usage.input_tokens"] == 5
    assert summary["attributes"]["eventide.retry_events"] == 1
    assert summary["attributes"]["eventide.usage.complete"] is False
    assert run["attributes"]["eventide.usage.input_tokens"] == 7
    assert run["attributes"]["eventide.usage.output_tokens"] == 10
    assert run["attributes"]["eventide.usage.complete"] is False


def test_old_summary_usage_and_response_without_request_keep_missing_evidence():
    trace = build_run_trace(
        [
            event("run.started", 1),
            event(
                "context.compacted",
                2,
                {
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            ),
            event("model.response", 3, {"usage": {"input_tokens": -1, "output_tokens": True}}),
            event("run.completed", 4),
        ]
    )
    (summary,) = spans(trace, "summary")
    (model,) = spans(trace, "model")
    assert summary["start_time_ns"] == summary["end_time_ns"]
    assert summary["attributes"]["eventide.request_observed"] is False
    assert model["attributes"]["eventide.usage.complete"] is False
    assert "eventide.usage.input_tokens" not in model["attributes"]


def test_workers_share_parent_trace_and_keep_exclusive_usage():
    events = completed_events() + [
        event("worker.started", 5, {"child_run_id": "child_a"}),
        event("run.completed", 7, {"usage": {"input_tokens": 100, "output_tokens": 50}}),
        event(
            "run.admitted",
            1,
            {"parent_run_id": "run_a", "worker_role": "researcher"},
            run="child_a",
            session="child_session",
        ),
        event("model.request", 2, run="child_a", session="child_session"),
        event(
            "model.response",
            3,
            {"usage": {"input_tokens": 9, "output_tokens": 8}},
            run="child_a",
            session="child_session",
        ),
        event("run.completed", 4, run="child_a", session="child_session"),
        event("run.started", 1, run="unrelated", session="other_session"),
    ]
    trace = build_run_trace(events, run_id="run_a")
    runs = spans(trace, "run")
    assert len(runs) == 2
    parent = next(run for run in runs if not run["parent_span_id"])
    child = next(run for run in runs if run["parent_span_id"])
    assert child["parent_span_id"] == parent["span_id"]
    assert child["trace_id"] == parent["trace_id"]
    assert child["attributes"]["eventide.worker.role"] == "researcher"
    assert parent["attributes"]["eventide.usage.input_tokens"] == 4
    assert child["attributes"]["eventide.usage.input_tokens"] == 9
    assert parent["attributes"]["eventide.usage.scope"] == "exclusive"
    (alone,) = spans(build_run_trace(events, run_id="child_a"), "run")
    assert alone["parent_span_id"] == parent["span_id"]


def test_continuation_resolves_original_tool_without_matching_other_sessions():
    events = [
        event("run.started", 1),
        event("tool.prepared", 2, {"call_id": "x", "name": "read_file", "readonly": True}),
        event("run.interrupted", 3),
        event("run.admitted", 4, {"continuation_of": "run_a"}, run="run_b"),
        event("tool.abandoned", 5, {"call_id": "x"}, run="run_b"),
        event("run.completed", 6, run="run_b"),
        event("run.started", 1, run="other", session="other"),
        event(
            "tool.prepared", 2, {"call_id": "x", "name": "read_file"}, run="other", session="other"
        ),
    ]
    trace = build_run_trace(events)
    tools = spans(trace, "tool")
    assert {tool["status"] for tool in tools} == {"abandoned", "unknown"}
    resumed = next(run for run in spans(trace, "run") if run["links"])
    original = next(run for run in spans(trace, "run") if run["status"] == "interrupted")
    assert resumed["links"][0]["span_id"] == original["span_id"]
    assert resumed["parent_span_id"] is None


def test_reused_call_ids_resolve_the_newest_continuation_call():
    events = [
        event("run.started", 1),
        event("tool.prepared", 2, {"call_id": "x", "name": "read_file"}),
        event("run.interrupted", 3),
        event("run.started", 4, {"continuation_of": "run_a"}, run="run_b"),
        event("tool.prepared", 5, {"call_id": "x", "name": "read_file"}, run="run_b"),
        event("run.interrupted", 6, run="run_b"),
        event("run.started", 7, {"continuation_of": "run_b"}, run="run_c"),
        event("tool.abandoned", 8, {"call_id": "x"}, run="run_c"),
    ]
    (original,) = spans(build_run_trace(events, run_id="run_a"), "tool")
    (newer,) = spans(build_run_trace(events, run_id="run_b"), "tool")
    assert original["status"] == "unknown"
    assert newer["status"] == "abandoned"


def test_partial_events_and_clock_regression_do_not_create_false_results():
    events = completed_events()
    events[2]["partial"] = True
    events[4]["ts"] = 0.5
    trace = build_run_trace(events)
    (model,) = spans(trace, "model")
    (tool,) = spans(trace, "tool")
    assert model["status"] == "unknown"
    assert tool["end_time_ns"] == tool["start_time_ns"]


@pytest.mark.parametrize(
    "events,match",
    [
        ([event("run.started", 1, {"parent_run_id": "run_a"})], "cycle"),
        (
            [event("run.started", 1), event("run.started", 2, session="conflict")],
            "conflicting Session",
        ),
        ([event("run.started", 1, ts=float("nan"))], "canonical events"),
        (
            [
                event("worker.started", 1, {"child_run_id": "child"}),
                event("run.started", 1, {"parent_run_id": "other"}, run="child", session="child"),
            ],
            "conflicting parent",
        ),
    ],
)
def test_invalid_event_identity_or_hierarchy_is_rejected(events, match):
    with pytest.raises(ValueError, match=match):
        build_run_trace(events)


def test_missing_run_is_rejected_and_empty_projection_is_valid():
    assert build_run_trace([])["spans"] == []
    with pytest.raises(ValueError, match="no canonical events"):
        build_run_trace([], run_id="missing")
    (run,) = spans(
        build_run_trace(
            [
                event("run.started", 1, {"continuation_of": "unobserved_parent"}),
            ]
        ),
        "run",
    )
    assert run["links"] == []
    assert run["attributes"]["eventide.continuation.observed"] is False


def test_import_and_json_projection_do_not_require_otel(monkeypatch):
    original = builtins.__import__

    def without_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("optional SDK is unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_otel)
    importlib.reload(telemetry)
    assert build_run_trace(completed_events())["spans"]
    with pytest.raises(RuntimeError, match="optional opentelemetry-sdk"):
        export_run_trace(completed_events(), exporter=object())


def test_otel_export_preserves_ids_links_times_and_has_no_global_side_effects():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    events = completed_events() + [
        event("run.started", 7, {"continuation_of": "run_a"}, run="run_b"),
        event("run.completed", 8, run="run_b"),
    ]
    projection = build_run_trace(events)
    provider = trace.get_tracer_provider()
    exporter = InMemorySpanExporter()
    assert export_run_trace(events, exporter=exporter) == len(projection["spans"])
    exported = exporter.get_finished_spans()
    assert trace.get_tracer_provider() is provider
    by_id = {f"{span.context.span_id:016x}": span for span in exported}
    for projected in projection["spans"]:
        span = by_id[projected["span_id"]]
        assert f"{span.context.trace_id:032x}" == projected["trace_id"]
        assert span.start_time == projected["start_time_ns"]
        assert span.end_time == projected["end_time_ns"]
        assert span.attributes == projected["attributes"]
        assert dict(span.resource.attributes) == {"service.name": "eventide"}
        if projected["parent_span_id"]:
            assert f"{span.parent.span_id:016x}" == projected["parent_span_id"]
        if projected["links"]:
            assert f"{span.links[0].context.span_id:016x}" == projected["links"][0]["span_id"]
    assert export_run_trace(events, exporter=exporter) == len(exported)
    assert {span.context.span_id for span in exporter.get_finished_spans()} == {
        span.context.span_id for span in exported
    }


def test_otel_failure_propagates_and_exporter_lifecycle_belongs_to_caller():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SpanExportResult

    class FailingExporter:
        def export(self, spans):
            assert spans
            return SpanExportResult.FAILURE

        def shutdown(self):
            pytest.fail("caller owns the exporter lifecycle")

    with pytest.raises(RuntimeError, match="rejected the trace"):
        export_run_trace(completed_events(), exporter=FailingExporter())
