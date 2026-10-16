"""Trace reads remain exact, private and independent of network exporters."""

import json

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.models import RunRequest
from eventide.telemetry import collect_trace_events
from tests.test_delivery_repair import done
from tests.test_delivery_runtime import delivery_host


def test_trace_read_download_and_unrelated_session_history(tmp_path):
    host = delivery_host(tmp_path, [done(), done()])
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        first = client.portal.call(host.run, RunRequest("private-task-content", session))
        initial = client.get(f"/api/runs/{first.run_id}/trace")
        assert initial.status_code == 200, initial.text
        assert {span["name"] for span in initial.json()["spans"]} >= {
            "eventide.run", "eventide.model",
        }
        client.portal.call(host.run, RunRequest("unrelated-task-content", session))
        before = host.store.session_events(session)
        later = client.get(f"/api/runs/{first.run_id}/trace")
        assert later.json() == initial.json()
        downloaded = client.get(f"/api/runs/{first.run_id}/trace?download=true")
        assert downloaded.json() == initial.json()
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["cache-control"] == "no-store"
        assert host.store.session_events(session) == before
        for private in ("private-task-content", "unrelated-task-content", str(tmp_path),
                        first.run_id, session):
            assert private not in downloaded.text
        assert client.get("/api/runs/missing/trace").status_code == 404


def test_trace_related_runs_are_bounded_and_not_all_sessions(tmp_path):
    host = delivery_host(tmp_path)
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        for n in range(70):
            run = f"run_{n}"
            host.store.create_run(run, session)
            host.store.append_fact(session, "run.started", {}, run_id=run)
        assert {e["run_id"] for e in collect_trace_events(host.store, "run_0")} == {"run_0"}
        for n in range(65):
            host.store.append_fact(session, "worker.started", {"child_run_id":f"run_{n+1}"},
                                   run_id=f"run_{n}")
        response = client.get("/api/runs/run_0/trace")
        assert response.status_code == 409 and "limit" in response.text
        assert "run_65" not in json.dumps(response.json())


def test_trace_child_selection_retains_parent_identity_without_sibling_spans(tmp_path):
    host = delivery_host(tmp_path)
    with TestClient(create_app(host)) as client:
        for run in ("root", "left", "right"):
            session = host.create_session()
            host.store.create_run(run, session)
            host.store.append_fact(session, "run.started",
                                   {"parent_run_id": "root"} if run != "root" else {},
                                   run_id=run)
        root_session = host.store.get_run_identity("root")["session_id"]
        for child in ("left", "right"):
            host.store.append_fact(root_session, "worker.started", {"child_run_id": child},
                                   run_id="root")
        whole = client.get("/api/runs/root/trace").json()
        child = client.get("/api/runs/left/trace").json()
        assert len(whole["spans"]) == 3 and len(child["spans"]) == 1
        assert child["spans"][0] in whole["spans"]
        collected = collect_trace_events(host.store, "left")
        assert {e["run_id"] for e in collected} == {"root", "left"}
        # A separate continuation is a link and does not expand the original trace.
        owner = host.create_session()
        host.store.create_run("continued", owner, continuation_of="left")
        host.store.append_fact(owner, "run.started", {"continuation_of": "left"},
                               run_id="continued")
        continued = client.get("/api/runs/continued/trace").json()
        assert len(continued["spans"]) == 1
        assert continued["spans"][0]["links"]
        assert client.get("/api/runs/left/trace").json() == child
