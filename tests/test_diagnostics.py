"""Targeted coverage for the fact-derived run diagnostics report (MK-19 slice).

The report must stay a pure projection of committed events: no model or tool
execution, no fabricated causes, bounded redacted text, and a stable
(run_id, high_water) binding that later events cannot pollute.
"""

import json
import uuid

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.diagnostics import (
    REPORT_VERSION,
    build_run_diagnostics,
    collect_run_diagnostics,
    render_diagnostics_markdown,
)
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from tests.test_runtime import settings_for


def make_store(tmp_path):
    return RuntimeStore(tmp_path / "diagnostics.sqlite3")


def admit(store, run_id="run-1", session="sess-1"):
    store.create_run(run_id, session)
    store.append_event(run_id, "run.admitted", {
        "mode": "auto", "model": "scripted", "provider": "scripted",
    })
    store.append_event(run_id, "run.started", {"mode": "auto"})


def report_for(store, run_id="run-1", high_water=None):
    events, bound = collect_run_diagnostics(store, run_id, high_water)
    return build_run_diagnostics(events, run_id=run_id, high_water=bound)


def test_completed_run_describes_facts_without_payload_content(tmp_path):
    store = make_store(tmp_path)
    admit(store)
    store.append_event("run-1", "model.response", {
        "step": 1, "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    store.append_event("run-1", "tool.prepared", {
        "call_id": "call-1", "name": "read_file", "readonly": True,
    })
    store.append_event("run-1", "tool.completed", {
        "call_id": "call-1", "is_error": False,
        "metadata": {"duration_ms": 3.0},
        "result": "SECRET-TOOL-RESULT-BODY",
    })
    store.append_event("run-1", "run.completed", {
        "output": "SECRET-ANSWER-BODY", "steps": 1, "tool_calls": 1, "usage": {},
    })
    report = report_for(store)
    assert report["report_version"] == REPORT_VERSION
    assert report["status"] == "completed"
    assert report["facts_available"] is True
    assert report["counts"] == {
        "steps": 1, "tool_calls": 1, "tools_completed": 1, "tools_failed": 0,
        "tools_unknown": 0, "tools_abandoned": 0, "tools_unresolved": 0,
        "attempts": 0, "attempts_usage_unknown": 0,
    }
    assert "已完成" in report["summary"]["what_happened"]
    assert "无需恢复" in report["summary"]["next_steps"][0]
    assert report["summary"]["where_stopped"] is None
    dumped = json.dumps(report, ensure_ascii=False)
    markdown = render_diagnostics_markdown(report)
    for secret in ("SECRET-TOOL-RESULT-BODY", "SECRET-ANSWER-BODY"):
        assert secret not in dumped
        assert secret not in markdown
    assert "正常结束" in markdown
    assert "## 哪些结果未知" in markdown


def test_unknown_tool_outcome_is_pending_not_known(tmp_path):
    store = make_store(tmp_path)
    admit(store)
    store.append_event("run-1", "tool.prepared", {
        "call_id": "call-9", "name": "bash", "readonly": False,
    })
    store.append_event("run-1", "tool.outcome_unknown", {
        "call_id": "call-9", "name": "bash", "readonly": False,
        "error": "Host stopped before sandbox container cleanup was confirmed",
    })
    store.append_event("run-1", "run.interrupted", {
        "error": "Run cancelled; tool outcome unknown, execution blocked",
        "reason": "unknown_tool_outcome",
    })
    report = report_for(store)
    codes = {item["code"] for item in report["pending"]}
    assert "tool_unknown" in codes
    assert report["status"] == "interrupted"
    assert "工具结果未知" in report["summary"]["where_stopped"]
    assert any("核查" in step for step in report["summary"]["next_steps"])
    assert any("Continue" not in step for step in report["summary"]["next_steps"])
    known_codes = {cause["code"] for cause in report["known_causes"]}
    assert "stop_reason" in known_codes
    assert "tool_unknown" not in known_codes


def test_budget_and_cancel_interruptions_offer_continue(tmp_path):
    for reason in ("token_budget", "step_budget", "cancelled"):
        store = make_store(tmp_path / reason)
        admit(store, run_id=f"run-{reason}")
        store.append_event(f"run-{reason}", "run.interrupted", {
            "error": "parked", "reason": reason,
        })
        report = report_for(store, run_id=f"run-{reason}")
        assert report["status"] == "interrupted"
        assert any("Continue" in step for step in report["summary"]["next_steps"])


def test_failed_run_records_known_cause_bounded(tmp_path):
    store = make_store(tmp_path)
    admit(store)
    store.append_event("run-1", "model.retry", {
        "attempt_id": "attempt_1", "attempt": 1, "retryable": False,
        "error": "x" * 5_000,
    })
    store.append_event("run-1", "run.failed", {"error": "boom"})
    report = report_for(store)
    retry = next(c for c in report["known_causes"] if c["code"] == "model_request_error")
    assert retry["detail"] is not None
    assert len(retry["detail"]) < 400
    assert "truncated" in retry["detail"]
    assert report["status"] == "failed"
    assert any("重新发起" in step for step in report["summary"]["next_steps"])
    assert report["truncated"] is True


def test_missing_events_show_unavailable_without_fabrication(tmp_path):
    store = make_store(tmp_path)
    store.create_run("run-empty", "sess-empty")
    report = report_for(store, run_id="run-empty")
    assert report["facts_available"] is False
    assert report["status"] == "unknown"
    assert report["summary"]["what_happened"].startswith("没有可用的事件记录")
    assert any("事件记录缺失" in item for item in report["unavailable"])
    markdown = render_diagnostics_markdown(report)
    assert "没有可用的事件记录" in markdown
    assert "不可用" in markdown


def test_unresolved_tool_and_attempt_stay_pending(tmp_path):
    store = make_store(tmp_path)
    admit(store)
    store.append_event("run-1", "model.attempt_started", {
        "attempt_id": "attempt_1", "attempt": 1, "kind": "agent", "model": "scripted",
    })
    store.append_event("run-1", "model.attempt_started", {
        "attempt_id": "attempt_2", "attempt": 2, "kind": "agent", "model": "scripted",
    })
    store.append_event("run-1", "model.attempt_finished", {
        "attempt_id": "attempt_2", "status": "failed", "usage_source": "unknown",
    })
    store.append_event("run-1", "tool.prepared", {
        "call_id": "call-2", "name": "edit_file", "readonly": False,
    })
    report = report_for(store)
    codes = {item["code"] for item in report["pending"]}
    assert {"tool_unresolved", "attempt_unfinished", "usage_unknown"} <= codes
    assert report["status"] == "unfinished"
    assert any("刷新" in step for step in report["summary"]["next_steps"])
    statuses = {row["attempt_id"]: row["status"] for row in report["attempts"]}
    assert statuses == {"attempt_1": "unknown", "attempt_2": "failed"}


def test_high_water_binds_report_against_new_events(tmp_path):
    store = make_store(tmp_path)
    admit(store)
    store.append_event("run-1", "tool.prepared", {
        "call_id": "call-1", "name": "glob", "readonly": True,
    })
    store.append_event("run-1", "tool.completed", {"call_id": "call-1", "is_error": False})
    before = report_for(store)
    assert before["high_water"] == 4
    store.append_event("run-1", "tool.prepared", {
        "call_id": "call-2", "name": "bash", "readonly": False,
    })
    events, bound = collect_run_diagnostics(store, "run-1", before["high_water"])
    assert bound == before["high_water"]
    replay = build_run_diagnostics(events, run_id="run-1", high_water=bound)
    assert replay == before
    fresh = report_for(store)
    assert fresh["high_water"] == 5
    assert fresh["counts"]["tools_unresolved"] == 1
    assert render_diagnostics_markdown(before) == render_diagnostics_markdown(replay)


def test_markdown_is_deterministic_and_covers_the_same_facts(tmp_path):
    store = make_store(tmp_path)
    admit(store)
    store.append_event("run-1", "tool.outcome_unknown", {
        "call_id": "call-1", "name": "bash", "readonly": False, "error": "cleanup lost",
    })
    store.append_event(
        "run-1", "run.interrupted",
        {"error": "blocked", "reason": "unknown_tool_outcome"},
    )
    report = report_for(store)
    first = render_diagnostics_markdown(report)
    assert first == render_diagnostics_markdown(report)
    assert f"session_seq ≤ {report['high_water']}" in first
    for cause in report["known_causes"]:
        assert cause["label"] in first
    for item in report["pending"]:
        assert item["label"] in first
    assert str(report["counts"]["tool_calls"]) in first


def _client(tmp_path, **setting_changes):
    runtime = RuntimeHost(
        settings_for(tmp_path, **setting_changes), ScriptedProvider([{"text": "ok"}]),
    )
    return TestClient(create_app(runtime)), runtime


def wait_terminal(client, run_id):
    import time

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = client.get(f"/api/runs/{run_id}").json()
        if record.get("status") not in {"queued", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_route_report_downloads_share_facts_and_never_execute(tmp_path):
    client, runtime = _client(tmp_path)
    with client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        record = wait_terminal(client, run_id)
        assert record["status"] == "completed"

        before_events = runtime.store.run_events(run_id)
        view = client.get(f"/api/runs/{run_id}/diagnostics")
        assert view.status_code == 200
        report = view.json()
        assert report["report_version"] == REPORT_VERSION
        assert report["status"] == "completed"
        assert report["high_water"] > 0
        assert runtime.store.run_events(run_id) == before_events
        assert runtime.store.get_run(run_id)["status"] == "completed"

        download_json = client.get(
            f"/api/runs/{run_id}/diagnostics/download?format=json&high_water={report['high_water']}"
        )
        assert download_json.status_code == 200
        assert download_json.headers["content-type"].startswith("application/json")
        assert "attachment" in download_json.headers["content-disposition"]
        body = download_json.json()
        assert body == report

        download_md = client.get(
            f"/api/runs/{run_id}/diagnostics/download?format=markdown&high_water={report['high_water']}"
        )
        assert download_md.status_code == 200
        assert download_md.headers["content-type"].startswith("text/markdown")
        assert "## 发生了什么" in download_md.text
        assert str(report["counts"]["tool_calls"]) in download_md.text

        cut = client.get(f"/api/runs/{run_id}/diagnostics?high_water=2")
        assert cut.status_code == 200
        assert cut.json()["high_water"] == 2
        assert cut.json() == client.get(
            f"/api/runs/{run_id}/diagnostics?high_water=2"
        ).json()


def test_route_error_contract(tmp_path):
    client, _runtime = _client(tmp_path)
    with client:
        assert client.get("/api/runs/missing/diagnostics").status_code == 404
        assert client.get(
            "/api/runs/missing/diagnostics/download?format=json"
        ).status_code == 404
        assert client.get("/api/runs/missing/diagnostics?high_water=3").status_code == 404


def test_route_rejects_invalid_parameters(tmp_path):
    client, runtime = _client(tmp_path)
    with client:
        store = runtime.store
        store.create_run("run-1", "sess-1")
        store.append_event("run-1", "run.admitted", {"mode": "auto"})
        base = "/api/runs/run-1/diagnostics"
        assert client.get(f"{base}?high_water=-1").status_code == 422
        assert client.get(f"{base}?high_water=not-a-number").status_code == 422
        assert client.get(
            f"{base}/download?format=xml"
        ).status_code == 422
        ok = client.get(base)
        assert ok.status_code == 200


def test_route_replaces_configured_key_in_event_text(tmp_path):
    client, runtime = _client(tmp_path, api_key="sk-eventide-secret")
    with client:
        store = runtime.store
        store.create_run("run-1", "sess-1")
        store.append_event("run-1", "run.admitted", {"mode": "auto", "model": "m"})
        # Write the raw text directly to bypass host-side cleaning, proving the
        # route applies the configured key normalization as defense in depth.
        store.append_fact("sess-1", "run.failed", {"error": "call failed with sk-eventide-secret"},
                          run_id="run-1")
        report = client.get("/api/runs/run-1/diagnostics").json()
        assert "sk-eventide-secret" not in json.dumps(report, ensure_ascii=False)
        assert "[REDACTED]" in json.dumps(report, ensure_ascii=False)
