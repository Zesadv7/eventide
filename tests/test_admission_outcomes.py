"""HTTP refusal is distinct from a failure after durable request acceptance."""

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_run_admission import host_for


def inject_after_admission(monkeypatch, host, failure):
    if failure == "used_cache":

        def fail(*_args, **_kwargs):
            raise OSError("post-commit used-cache failure")

        monkeypatch.setattr("eventide.host.attachments.mark_attachments_used", fail)
    else:

        def fail(*_args, **_kwargs):
            raise ValueError("post-commit dispatch failure")

        monkeypatch.setattr(host.dispatch, "start", fail)


@pytest.mark.parametrize("target", ["source", "snapshot", "worktree"])
@pytest.mark.parametrize("failure", ["used_cache", "dispatch"])
def test_accepted_run_failure_returns_identity_and_replays_after_restart(
    isolated_workspace,
    monkeypatch,
    target,
    failure,
):
    host = host_for(isolated_workspace)
    settings = host.settings
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        path = f"/api/sessions/{session}/runs"
        body = {
            "prompt": "keep one intent",
            "execution_target": target,
            "mode": "plan" if target == "snapshot" else "auto",
        }
        key = {"Idempotency-Key": "accepted-once"}
        inject_after_admission(monkeypatch, host, failure)
        first = client.post(path, json=body, headers=key)
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        assert host.store.get_run(run_id)["status"] == "failed"
        assert client.post(path, json=body, headers=key).json() == first.json()
        assert (
            client.post(path, json={**body, "prompt": "different"}, headers=key).status_code == 409
        )
        assert len(host.session_runs(session)) == 1
        assert [e["type"] for e in host.store.session_events(session)].count("message.user") == 1
        assert host.provider.requests == []
    monkeypatch.undo()
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(reopened)) as client:
        assert client.post(path, json=body, headers=key).json() == first.json()
        assert len(reopened.session_runs(session)) == 1
        assert reopened.provider.requests == []


@pytest.mark.parametrize("failure", ["used_cache", "dispatch"])
def test_accepted_continue_failure_replays_original_continuation(
    isolated_workspace,
    monkeypatch,
    failure,
):
    host = host_for(isolated_workspace)
    settings = host.settings
    with TestClient(create_app(host)) as client:
        session = host.create_session()
        old = client.portal.call(host.admit_run, RunRequest("one user message", session))
        client.portal.call(host.cancel_run, old)
        inject_after_admission(monkeypatch, host, failure)
        path = f"/api/sessions/{session}/continue"
        key = {"Idempotency-Key": "continue-once"}
        first = client.post(path, headers=key)
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        assert host.store.get_run(run_id)["status"] == "failed"
        assert host.store.get_run_identity(run_id)["continuation_of"] == old
        assert client.post(path, headers=key).json() == first.json()
        assert len(host.session_runs(session)) == 2
        assert [e["type"] for e in host.store.session_events(session)].count("message.user") == 1
        assert host.provider.requests == []
    monkeypatch.undo()
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(reopened)) as client:
        assert client.post(path, headers=key).json() == first.json()
        assert len(reopened.session_runs(session)) == 2
        assert reopened.provider.requests == []


@pytest.mark.parametrize("operation", ["runs", "continue"])
def test_only_unaccepted_preconditions_are_marked_rejected(
    isolated_workspace,
    monkeypatch,
    operation,
):
    host = host_for(isolated_workspace)
    with TestClient(create_app(host)) as client:
        session = host.create_session()

        async def refuse(*_args, **_kwargs):
            raise ValueError("Session is busy")

        monkeypatch.setattr(host, "admit_run" if operation == "runs" else "admit_continue", refuse)
        response = client.post(
            f"/api/sessions/{session}/{operation}",
            json={"prompt": "refused"} if operation == "runs" else None,
            headers={"Idempotency-Key": "refused"},
        )
        assert response.status_code == 409
        assert response.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        assert host.session_runs(session) == []
        assert host.provider.requests == []


@pytest.mark.parametrize("operation", ["runs", "continue"])
def test_unclassified_admission_fault_is_unknown_without_exposing_exception(
    isolated_workspace,
    monkeypatch,
    operation,
):
    host = host_for(isolated_workspace)
    with TestClient(create_app(host)) as client:
        session = host.create_session()

        async def fail(*_args, **_kwargs):
            raise RuntimeError("private fault detail")

        monkeypatch.setattr(host, "admit_run" if operation == "runs" else "admit_continue", fail)
        response = client.post(
            f"/api/sessions/{session}/{operation}",
            json={"prompt": "unknown"} if operation == "runs" else None,
            headers={"Idempotency-Key": "unknown"},
        )
        assert response.status_code == 503
        assert response.headers.get("X-Eventide-Mutation-Outcome") is None
        assert "unknown" in response.json()["detail"]
        assert "private fault detail" not in response.text
        assert host.session_runs(session) == []
        assert host.provider.requests == []
