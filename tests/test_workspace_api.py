"""Public workspace and continuation entry points."""

import asyncio
import json
import sqlite3
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.migration import import_v02_database
from eventide.models import RunRequest
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
        response = client.post(
            "/api/workspaces",
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"path": str(other)},
        )
        assert response.status_code == 201
        workspace_id = response.json()["workspace_id"]
        assert "git_branch" in response.json()
        assert "git_head" in response.json()
        assert client.get(f"/api/workspaces/{workspace_id}").json()["path"] == str(other)
        assert len(client.get("/api/workspaces").json()) == 2
        session = client.post(
            "/api/sessions",
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"workspace_id": workspace_id},
        ).json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "hello"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
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
        assert (
            client.delete(
                f"/api/workspaces/{workspace_id}", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).status_code
            == 409
        )
        assert client.get("/api/sessions/missing").status_code == 404
        assert client.get("/api/workspaces/missing").status_code == 404
        assert (
            client.post(
                "/api/sessions",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={"workspace_id": "missing"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/workspaces",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={"path": str(other / "absent")},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/sessions/{session}/continue", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).status_code
            == 409
        )


def test_http_continue_and_reject_parked_normal_run(isolated_workspace):
    host, session, _ = asyncio.run(interrupted_host(isolated_workspace))
    with TestClient(create_app(host)) as client:
        assert client.get(f"/api/sessions/{session}").json()["status"] == "parked"
        assert (
            client.post(
                f"/api/sessions/{session}/runs",
                json={"prompt": "oops"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ).status_code
            == 409
        )
        result = client.post(
            f"/api/sessions/{session}/continue", headers={"Idempotency-Key": uuid.uuid4().hex}
        )
        assert result.status_code == 202
        assert result.json()["status"] == "accepted"
        continued = wait_for_run(client, result.json()["run_id"])
        assert continued["continuation_of"] == "crashed"
        assert continued["status"] == "completed"


def test_http_continue_returns_before_provider_finishes(isolated_workspace):
    class WaitingProvider:
        async def complete(self, _request):
            await asyncio.Future()

    host, session, _ = asyncio.run(interrupted_host(isolated_workspace))
    host.provider = WaitingProvider()
    with TestClient(create_app(host)) as client:
        accepted = client.post(
            f"/api/sessions/{session}/continue", headers={"Idempotency-Key": uuid.uuid4().hex}
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        assert client.get(f"/api/runs/{run_id}").status_code == 200
        for _ in range(100):
            if host.store.run_events(run_id):
                break
            time.sleep(0.01)
        assert (
            client.post(
                f"/api/runs/{run_id}/cancel", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).status_code
            == 200
        )


def test_http_abandon_unlocks_parked_session(isolated_workspace):
    host, session, _ = asyncio.run(interrupted_host(isolated_workspace, "bash"))
    with TestClient(create_app(host)) as client:
        assert (
            client.post(
                f"/api/sessions/{session}/continue", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).status_code
            == 409
        )
        abandoned = client.post(
            f"/api/sessions/{session}/abandon",
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"expected_run_id": "crashed"},
        )
        assert abandoned.status_code == 200
        assert abandoned.json()["continuation_of"] == "crashed"
        assert client.get(f"/api/sessions/{session}").json()["status"] == "completed"


def test_http_session_metadata_archive_and_delete(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(host)) as client:
        workspace_id = host.default_workspace.workspace_id
        empty = client.post(
            "/api/sessions",
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"workspace_id": workspace_id},
        ).json()["session_id"]
        renamed = client.patch(
            f"/api/sessions/{empty}",
            json={
                "title": "Named work",
                "expected_metadata_hash": client.get(f"/api/sessions/{empty}").json()[
                    "metadata_hash"
                ],
            },
            headers={"Idempotency-Key": "rename"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Named work"

        archived = client.patch(
            f"/api/sessions/{empty}",
            json={"archived": True, "expected_metadata_hash": renamed.json()["metadata_hash"]},
            headers={"Idempotency-Key": "archive"},
        )
        assert archived.json()["archived"] is True
        assert client.get(f"/api/workspaces/{workspace_id}/sessions").json() == []
        assert (
            len(
                [
                    s
                    for s in client.get(
                        f"/api/workspaces/{workspace_id}/sessions?include_archived=true"
                    ).json()
                    if not s["managed"]
                ]
            )
            == 1
        )
        assert (
            client.request(
                "DELETE",
                f"/api/sessions/{empty}",
                json={"expected_metadata_hash": archived.json()["metadata_hash"]},
                headers={"Idempotency-Key": "delete-empty"},
            ).status_code
            == 200
        )

        history = client.post(
            "/api/sessions",
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"workspace_id": workspace_id},
        ).json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{history}/runs",
            json={"prompt": "work"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        wait_for_run(client, accepted.json()["run_id"])
        assert (
            client.request(
                "DELETE",
                f"/api/sessions/{history}",
                json={
                    "expected_metadata_hash": client.get(f"/api/sessions/{history}").json()[
                        "metadata_hash"
                    ]
                },
                headers={"Idempotency-Key": "delete-history"},
            ).status_code
            == 409
        )


def test_http_session_working_directory_scopes_execution(isolated_workspace):
    package = isolated_workspace / "package"
    package.mkdir()
    (package / "marker.txt").write_text("from package", encoding="utf-8")
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "read",
                        "name": "read_file",
                        "arguments": {"path": "marker.txt"},
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    with TestClient(create_app(host)) as client:
        workspace_id = host.default_workspace.workspace_id
        created = client.post(
            "/api/sessions",
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"workspace_id": workspace_id, "working_directory": "package"},
        )
        assert created.status_code == 201
        session = created.json()["session_id"]
        assert client.get(f"/api/sessions/{session}").json()["working_directory"] == str(package)
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "read marker"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        run_id = accepted.json()["run_id"]
        assert wait_for_run(client, run_id)["status"] == "completed"
        result = next(
            event for event in host.store.run_events(run_id) if event["type"] == "tool.completed"
        )
        assert result["payload"]["content"] == "from package"
        assert f"Working directory: {package}" in provider.requests[0].system
        assert (
            client.post(
                "/api/sessions",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={"workspace_id": workspace_id, "working_directory": ".."},
            ).status_code
            == 422
        )


async def test_python_workspace_and_run_interfaces(isolated_workspace):
    host = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "python done"}])
    )
    try:
        other = isolated_workspace / "other"
        other.mkdir()
        workspace = host.resolve_or_register_workspace(other)
        assert len(host.store.list_workspaces()) == 2
        assert host.workspace_status(workspace.workspace_id)["path"] == str(other)
        host.store.remove_workspace(workspace.workspace_id)
        assert other.is_dir()
        with pytest.raises(ValueError, match="Workspace not found"):
            host.workspace_status("missing")
        workspace = host.resolve_or_register_workspace(other)
        session = host.create_session(workspace_id=workspace.workspace_id)
        result = await host.run(RunRequest("hello", session))
        assert result.output == "python done"
        with pytest.raises(ValueError, match="existing Session"):
            await host.continue_session("missing")
    finally:
        await host.close()


async def test_python_continue(isolated_workspace):
    host, session, _ = await interrupted_host(isolated_workspace)
    try:
        assert (await host.continue_session(session)).continuation_of == "crashed"
    finally:
        await host.close()


async def test_python_abandon(isolated_workspace):
    host, session, _ = await interrupted_host(isolated_workspace, "bash")
    try:
        assert (await host.abandon_interruption(session)).continuation_of == "crashed"
    finally:
        await host.close()


def test_python_imports_v02_history_without_changing_source(isolated_workspace):
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
    host = RuntimeHost(settings, ScriptedProvider([]))
    workspace_id = host.default_workspace.workspace_id
    try:
        result = import_v02_database(host.store, source, workspace_id)
        assert result["sessions"] == 1 and result["api_key_imported"] is False
        assert source.read_bytes() == original
    finally:
        asyncio.run(host.close())
    store = RuntimeStore(settings.state_dir / "runtime.sqlite")
    try:
        assert store.load_messages("old-session") == [
            {"role": "user", "content": "old prompt"},
            {"role": "assistant", "content": "old answer"},
        ]
        assert store.get_run("old-run")["output"] == "old answer"
        assert store.get_provider_config() is None
        assert bool(store.get_session("old-session")["archived"])
        assert sum(event["type"] == "run.completed" for event in store.run_events("old-run")) == 1
        with pytest.raises(ValueError, match="already exists"):
            import_v02_database(store, source, workspace_id)
    finally:
        store.close()


async def test_python_exports_canonical_jsonl(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "exported"}]))
    try:
        result = await host.run(RunRequest("keep history"))
        destination = isolated_workspace / "audit.jsonl"
        host.store.export_jsonl(result.run_id, destination)
        events = [json.loads(line) for line in destination.read_text().splitlines()]
        assert events[-1]["type"] == "run.completed"
        assert events == host.store.run_events(result.run_id)
    finally:
        await host.close()
