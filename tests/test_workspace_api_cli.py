"""Public workspace and continuation entry points."""

import asyncio
import json

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.cli import main
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_host import interrupted_host
from tests.test_runtime import settings_for


def test_workspace_http_catalog_and_session_binding(isolated_workspace):
    other = isolated_workspace / "other"
    other.mkdir()
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(host)) as client:
        response = client.post("/api/workspaces", json={"path": str(other)})
        assert response.status_code == 201
        workspace_id = response.json()["workspace_id"]
        assert "git_branch" in response.json()
        assert "git_head" in response.json()
        assert client.get(f"/api/workspaces/{workspace_id}").json()["path"] == str(other)
        assert len(client.get("/api/workspaces").json()) == 2
        session = client.post("/api/sessions", json={"workspace_id": workspace_id}).json()[
            "session_id"
        ]
        accepted = client.post(f"/api/sessions/{session}/runs", json={"prompt": "hello"})
        run_id = accepted.json()["run_id"]
        assert wait_for_run(client, run_id)["status"] == "completed"
        assert client.get(f"/api/sessions/{session}/messages").json()[0]["content"] == "hello"
        history = client.get(f"/api/sessions/{session}/runs")
        assert history.status_code == 200
        assert history.json()[0]["id"] == run_id
        assert history.json()[0]["event_count"] > 0
        assert (
            client.get(f"/api/workspaces/{workspace_id}/sessions").json()[0]["session_id"]
            == session
        )
        assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 409
        assert client.get("/api/sessions/missing").status_code == 404
        assert client.get("/api/workspaces/missing").status_code == 404
        assert client.post("/api/sessions", json={"workspace_id": "missing"}).status_code == 404
        assert (
            client.post("/api/workspaces", json={"path": str(other / "absent")}).status_code == 422
        )
        assert client.post(f"/api/sessions/{session}/continue").status_code == 409


def test_http_continue_and_reject_parked_normal_run(isolated_workspace):
    host, session, _ = asyncio.run(interrupted_host(isolated_workspace))
    with TestClient(create_app(host)) as client:
        assert client.get(f"/api/sessions/{session}").json()["status"] == "parked"
        assert (
            client.post(f"/api/sessions/{session}/runs", json={"prompt": "oops"}).status_code == 409
        )
        result = client.post(f"/api/sessions/{session}/continue")
        assert result.status_code == 200
        assert result.json()["continuation_of"] == "crashed"
        assert result.json()["status"] == "completed"


def test_http_abandon_unlocks_parked_session(isolated_workspace):
    host, session, _ = asyncio.run(interrupted_host(isolated_workspace, "bash"))
    with TestClient(create_app(host)) as client:
        assert client.post(f"/api/sessions/{session}/continue").status_code == 409
        abandoned = client.post(f"/api/sessions/{session}/abandon")
        assert abandoned.status_code == 200
        assert abandoned.json()["continuation_of"] == "crashed"
        assert client.get(f"/api/sessions/{session}").json()["status"] == "completed"


def test_http_session_metadata_archive_and_delete(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(host)) as client:
        workspace_id = host.default_workspace.workspace_id
        empty = client.post("/api/sessions", json={"workspace_id": workspace_id}).json()[
            "session_id"
        ]
        renamed = client.patch(
            f"/api/sessions/{empty}",
            json={"title": "Named work"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Named work"

        archived = client.patch(
            f"/api/sessions/{empty}",
            json={"archived": True},
        )
        assert archived.json()["archived"] is True
        assert client.get(f"/api/workspaces/{workspace_id}/sessions").json() == []
        assert len(
            client.get(
                f"/api/workspaces/{workspace_id}/sessions?include_archived=true"
            ).json()
        ) == 1
        assert client.delete(f"/api/sessions/{empty}").status_code == 200

        history = client.post(
            "/api/sessions", json={"workspace_id": workspace_id}
        ).json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{history}/runs", json={"prompt": "work"}
        )
        wait_for_run(client, accepted.json()["run_id"])
        assert client.delete(f"/api/sessions/{history}").status_code == 409


def test_cli_workspace_and_runs(isolated_workspace, monkeypatch, capsys):
    import eventide.cli as cli

    settings = settings_for(isolated_workspace)
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(
        cli,
        "AgentRuntime",
        lambda settings: RuntimeHost(
            settings,
            ScriptedProvider([{"text": "cli done"}]),
        ),
    )
    other = isolated_workspace / "other"
    other.mkdir()
    assert main(["workspace", "add", str(other)]) == 0
    workspace_id = json.loads(capsys.readouterr().out)["workspace_id"]
    assert main(["workspace", "list"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["workspace", "show", workspace_id]) == 0
    assert json.loads(capsys.readouterr().out)["path"] == str(other)
    assert main(["workspace", "remove", workspace_id]) == 0
    capsys.readouterr()
    assert other.is_dir()
    assert main(["workspace", "show", "missing"]) == 1
    capsys.readouterr()
    assert main(["run", "hello", "--workspace", str(other), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["output"] == "cli done"
    answers = iter(["hello", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert main(["chat", "--workspace", str(other)]) == 0
    assert "cli done" in capsys.readouterr().out
    assert main(["continue", "missing"]) == 1


def test_cli_continue(isolated_workspace, monkeypatch, capsys):
    import eventide.cli as cli

    host, session, _ = asyncio.run(interrupted_host(isolated_workspace))
    monkeypatch.setattr(cli, "AgentRuntime", lambda _: host)
    assert main(["continue", session, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["continuation_of"] == "crashed"


def test_cli_abandon(isolated_workspace, monkeypatch, capsys):
    import eventide.cli as cli

    host, session, _ = asyncio.run(interrupted_host(isolated_workspace, "bash"))
    monkeypatch.setattr(cli, "AgentRuntime", lambda _: host)
    assert main(["abandon", session, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["continuation_of"] == "crashed"
