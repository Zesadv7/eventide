from eventide.diagnostics import render_diagnostics_markdown
from tests.test_diagnostics import admit, make_store, report_for


def test_display_limit_does_not_change_complete_run_counts(tmp_path):
    store = make_store(tmp_path)
    admit(store)
    for i in range(102):
        store.append_event(
            "run-1", "tool.prepared", {"call_id": str(i), "name": "read_file", "readonly": True}
        )
        store.append_event("run-1", "tool.completed", {"call_id": str(i), "is_error": False})
        store.append_event("run-1", "model.attempt_started", {"attempt_id": str(i)})
        store.append_event(
            "run-1",
            "model.attempt_finished",
            {"attempt_id": str(i), "status": "completed", "usage_source": "reported"},
        )
    report = report_for(store)
    assert report["counts"]["tools_completed"] == 102
    assert report["counts"]["attempts"] == 102
    assert len(report["tools"]) == len(report["attempts"]) == 100
    assert report["truncated"]


def test_missing_events_never_render_as_normal_completion(tmp_path):
    store = make_store(tmp_path)
    store.create_run("run-1", "sess-1")
    assert "正常结束" not in render_diagnostics_markdown(report_for(store))
