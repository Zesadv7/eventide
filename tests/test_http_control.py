"""Durable request de-duplication and browser mutation origin boundaries."""

import time

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def wait(client, run):
    for _ in range(200):
        result = client.get(f"/api/runs/{run}").json()
        if result["status"] in {"completed", "interrupted", "failed"}:
            return result
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_run_key_is_persistent_and_content_bound(isolated_workspace):
    settings = settings_for(isolated_workspace)
    provider = ScriptedProvider([{"text": "done"}])
    host = RuntimeHost(settings, provider)
    with TestClient(create_app(host)) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        path = f"/api/sessions/{sid}/runs"
        header = {"Idempotency-Key": "same-submit"}
        first = client.post(path, json={"prompt": "work"}, headers=header)
        assert first.status_code == 202
        wait(client, first.json()["run_id"])
        assert client.post(path, json={"prompt": "work"}, headers=header).json() == first.json()
        assert client.post(path, json={"prompt": "other"}, headers=header).status_code == 409
        assert len(provider.requests) == 1
    reopened_provider = ScriptedProvider([])
    reopened = RuntimeHost(settings, reopened_provider)
    with TestClient(create_app(reopened)) as client:
        assert client.post(path, json={"prompt": "work"}, headers=header).json() == first.json()
        assert reopened_provider.requests == []


def test_continue_key_does_not_create_another_turn(isolated_workspace):
    from dataclasses import replace

    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state", max_steps=1)
    host = RuntimeHost(
        settings,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {"id": "read", "name": "read_file", "arguments": {"path": "missing"}}
                    ]
                },
                {"text": "finished"},
            ]
        ),
    )
    with TestClient(create_app(host)) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        initial = client.post(f"/api/sessions/{sid}/runs", json={"prompt": "work"}).json()
        assert wait(client, initial["run_id"])["status"] == "interrupted"
        path = f"/api/sessions/{sid}/continue"
        header = {"Idempotency-Key": "continue-once"}
        first = client.post(path, headers=header)
        assert first.status_code == 202
        assert wait(client, first.json()["run_id"])["status"] == "completed"
        assert client.post(path, headers=header).json() == first.json()
        assert len(client.get(f"/api/sessions/{sid}/runs").json()) == 2


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://attacker.example", "Host": "attacker.example"},
        {"Origin": "http://testserver:bad"},
    ],
)
def test_cross_site_mutation_is_rejected_without_changes(isolated_workspace, headers):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        assert client.post("/api/sessions", headers=headers).status_code == 403
        assert client.get(
            f"/api/workspaces/{host.default_workspace.workspace_id}/sessions"
        ).status_code == 200
        assert host.store.list_sessions(host.default_workspace.workspace_id) == []


def test_same_origin_and_header_validation(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        created = client.post("/api/sessions", headers={"Origin": "http://testserver"})
        assert created.status_code == 201
        sid = created.json()["session_id"]
        assert (
            client.post(
                f"/api/sessions/{sid}/runs",
                json={"prompt": "work"},
                headers={"Idempotency-Key": "contains space"},
            ).status_code
            == 422
        )
