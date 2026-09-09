"""Public workspace and continuation entry points."""

import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.cli import main
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
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
        assert client.get(f"/api/sessions/{session}/runs?limit=0").status_code == 422
        assert client.get("/api/runs/missing/events").status_code == 404
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


def test_cli_imports_v02_history_without_changing_source(
    isolated_workspace, monkeypatch, capsys
):
    import eventide.cli as cli

    source = isolated_workspace / "nexus.db"
    connection = sqlite3.connect(source)
    connection.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE messages (
            session_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,
            content_json TEXT NOT NULL, PRIMARY KEY (session_id, seq)
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at REAL NOT NULL, completed_at REAL, output TEXT NOT NULL DEFAULT '',
            steps INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
            duration_ms REAL NOT NULL DEFAULT 0, usage_json TEXT NOT NULL DEFAULT '{}',
            error TEXT
        );
        CREATE TABLE events (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
            type TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (run_id, seq)
        );
        CREATE TABLE provider_config (
            id INTEGER PRIMARY KEY, provider TEXT NOT NULL, base_url TEXT,
            model TEXT NOT NULL, api_key_ciphertext TEXT, updated_at REAL NOT NULL
        );
        INSERT INTO sessions VALUES ('old-session', 10, 20);
        INSERT INTO messages VALUES ('old-session', 1, 'user', '"old prompt"');
        INSERT INTO messages VALUES ('old-session', 2, 'assistant', '"old answer"');
        INSERT INTO runs VALUES (
            'old-run', 'old-session', 'completed', 11, 12, 'old answer',
            1, 0, 25, '{"input_tokens": 3, "output_tokens": 2}', NULL
        );
        INSERT INTO events VALUES (
            'old-run', 1, 11, 'run.started', '{"provider": "anthropic"}'
        );
        INSERT INTO events VALUES (
            'old-run', 2, 12, 'run.completed', '{"output": "old answer"}'
        );
        INSERT INTO provider_config VALUES (
            1, 'anthropic', NULL, 'legacy-model', 'old-ciphertext', 12
        );
    """)
    connection.close()
    original = source.read_bytes()

    settings = settings_for(isolated_workspace)
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(
        cli,
        "AgentRuntime",
        lambda value: RuntimeHost(value, ScriptedProvider([{"text": "unused"}])),
    )
    assert main(["migrate-v02", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sessions"] == 1
    assert result["api_key_imported"] is False
    assert source.read_bytes() == original

    store = RuntimeStore(settings.state_dir / "runtime.sqlite")
    assert store.load_messages("old-session") == [
        {"role": "user", "content": "old prompt"},
        {"role": "assistant", "content": "old answer"},
    ]
    assert store.get_run("old-run")["output"] == "old answer"
    assert store.get_provider_config()["api_key_ciphertext"] is None
    assert sum(event["type"] == "run.completed" for event in store.run_events("old-run")) == 1
    store.close()

    assert main(["migrate-v02", str(source)]) == 1
    assert "already exists" in capsys.readouterr().out
