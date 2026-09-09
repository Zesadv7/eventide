"""Tests for trace persistence and redaction."""

from eventide.observability import TraceStore, redact


def test_redact_secrets_and_large_values():
    result = redact(
        {
            "api_key": "secret",
            "nested": {"token": "abc"},
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "text": "x" * 5000,
        }
    )
    assert result["api_key"] == "[REDACTED]"
    assert result["nested"]["token"] == "[REDACTED]"
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert "truncated" in result["text"]


def test_trace_store_roundtrip(isolated_workspace):
    store = TraceStore(isolated_workspace / "trace.db")
    store.create_session("s1")
    store.append_message("s1", {"role": "user", "content": "hello"})
    store.create_run("r1", "s1")
    store.append_event("r1", "run.started", {"api_key": "hidden"})
    store.finish_run(
        "r1",
        status="completed",
        output="done",
        steps=1,
        tool_calls=0,
        duration_ms=2.5,
        usage={"input_tokens": 1},
    )
    assert store.load_messages("s1")[0]["content"] == "hello"
    assert store.get_run("r1")["status"] == "completed"
    assert store.get_events("r1")[0]["payload"]["api_key"] == "[REDACTED]"
    destination = store.export_jsonl("r1", isolated_workspace / "trace.jsonl")
    assert "run.started" in destination.read_text(encoding="utf-8")
    store.close()
