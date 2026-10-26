import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_host import make_repo
from tests.test_runtime import settings_for


@pytest.mark.parametrize("target", ["source", "worktree", "snapshot"])
def test_continue_binds_exact_source_and_replays_same_receipt(isolated_workspace, target):
    repo = make_repo(isolated_workspace / "repo")
    settings = settings_for(repo, max_steps=1, state_dir=isolated_workspace / "runtime-state")
    host = RuntimeHost(settings, ScriptedProvider([
        {"tool_calls": [{"id": "g1", "name": "glob", "arguments": {"pattern": "**/*"}}]},
        {"text": "done"},
    ]))
    with TestClient(create_app(host)) as client:
        session = client.post("/api/sessions", headers={"Idempotency-Key": "session"}).json()[
            "session_id"
        ]
        first = client.post(f"/api/sessions/{session}/runs", json={
            "prompt": "inspect source", "execution_target": target,
            "mode": "plan" if target == "snapshot" else "auto",
        }, headers={"Idempotency-Key": "first"})
        assert first.status_code == 202, first.text
        original = first.json()["run_id"]
        assert wait_for_run(client, original)["status"] == "interrupted"
        endpoint = f"/api/sessions/{session}/continue"
        rejected = client.post(endpoint, json={"expected_run_id": "not-this-run"},
                               headers={"Idempotency-Key": "wrong-source"})
        assert rejected.status_code == 409
        assert rejected.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        assert host.store.latest_run_summary(session)["id"] == original
        accepted = client.post(endpoint, json={"expected_run_id": original},
                               headers={"Idempotency-Key": "continue-source"})
        assert accepted.status_code == 202
        assert wait_for_run(client, accepted.json()["run_id"])["status"] == "completed"
        replay = client.post(endpoint, json={"expected_run_id": original},
                             headers={"Idempotency-Key": "continue-source"})
        assert replay.json() == accepted.json()
        changed = client.post(endpoint, json={"expected_run_id": "changed"},
                              headers={"Idempotency-Key": "continue-source"})
        assert changed.status_code == 409
        late = client.post(endpoint, json={"expected_run_id": original},
                           headers={"Idempotency-Key": "late-click"})
        assert late.status_code == 409
