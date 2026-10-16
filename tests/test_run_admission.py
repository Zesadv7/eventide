"""Accepted requests survive worker scheduling, cancellation and process death."""

import asyncio
import json
import subprocess
import sys
import threading
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from eventide import attachments
from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def host_for(path, provider=None):
    repo = make_repo(path / "repo")
    return RuntimeHost(
        replace(settings_for(repo), state_dir=path / "state"), provider or ScriptedProvider([])
    )


async def test_admission_can_cancel_before_scheduling_and_keeps_target(isolated_workspace):
    provider = ScriptedProvider([{"text": "continued"}])
    host = host_for(isolated_workspace, provider)
    try:
        session = host.create_session()
        run_id = await host.admit_run(RunRequest("keep this goal", session, mode="plan"))
        assert host.store.get_run(run_id)["status"] == "queued"
        assert host.store.load_messages(session) == [{"role": "user", "content": "keep this goal"}]
        with pytest.raises(ValueError, match="active runs"):
            await host.configure_provider(
                provider="anthropic", api_key=None, base_url=None, model="m"
            )
        with pytest.raises(ValueError, match="busy"):
            await host.admit_run(RunRequest("must not slip in", session))
        stopped = await host.cancel_run(run_id)
        assert stopped["status"] == "interrupted"
        assert provider.requests == []
        assert not host._admissions
        with pytest.raises(ValueError, match="awaiting execution"):
            host.start_admitted_run(run_id)
        result = await host.continue_session(session)
        assert result.status == "completed"
        assert provider.requests[0].messages[0]["content"] == "keep this goal"
        assert not any(tool["name"] == "bash" for tool in provider.requests[0].tools)
    finally:
        await host.close()


async def test_scheduled_worker_can_be_cancelled_before_first_turn(isolated_workspace):
    provider = ScriptedProvider([])
    host = host_for(isolated_workspace, provider)
    try:
        session = host.create_session()
        run_id = await host.admit_run(RunRequest("do not lose me", session))
        task = host.start_admitted_run(run_id)
        stopped = await host.cancel_run(run_id)
        assert task.cancelled()
        assert stopped["status"] == "interrupted"
        assert provider.requests == []
        assert not host._admissions
        assert host.store.load_messages(session)[0]["content"] == "do not lose me"
    finally:
        await host.close()


async def test_duplicate_identity_cannot_cancel_another_workspace_admission(isolated_workspace):
    host = host_for(isolated_workspace)
    try:
        first = host.create_session()
        other = host.resolve_or_register_workspace(make_repo(isolated_workspace / "other"))
        second = host.create_session(workspace_id=other.workspace_id)
        await host.admit_run(RunRequest("first goal", first, "same-id"))
        with pytest.raises(ValueError, match="already used"):
            await host.admit_run(RunRequest("another goal", second, "same-id"))
        assert host.store.get_run("same-id")["status"] == "queued"
        assert host.workspace_busy(host.default_workspace.workspace_id)
        assert host.store.load_messages(second) == []
    finally:
        await host.close()


@pytest.mark.parametrize("other_workspace", [False, True])
async def test_direct_run_cannot_steal_pending_admission(
    isolated_workspace, monkeypatch, other_workspace
):
    host = host_for(isolated_workspace)
    first = host.create_session()
    workspace = host.default_workspace
    if other_workspace:
        workspace = host.resolve_or_register_workspace(make_repo(isolated_workspace / "other"))
    second = host.create_session(workspace_id=workspace.workspace_id)
    entered = threading.Event()
    release = threading.Event()
    from eventide import host as host_module

    original = host_module.workspace_checkpoint

    def checkpoint(root, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(root, **kwargs)

    monkeypatch.setattr(host_module, "workspace_checkpoint", checkpoint)
    admission = asyncio.create_task(host.admit_run(RunRequest("first", first, "reserved")))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(ValueError, match="already used"):
            await host.run(RunRequest("second", second, "reserved"))
        assert host._admission_done["reserved"].session_id == first
        release.set()
        assert await admission == "reserved"
        assert host.store.get_run_identity("reserved")["session_id"] == first
        assert host.store.load_messages(second) == []
        await host.cancel_run("reserved")
    finally:
        release.set()
        await host.close()


async def test_host_close_waits_for_admission_thread(isolated_workspace, monkeypatch):
    host = host_for(isolated_workspace)
    session = host.create_session()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    from eventide import host as host_module

    def checkpoint(root, **kwargs):
        entered.set()
        assert release.wait(5)
        # The database owner must still be alive while this thread completes.
        assert host.store.get_session(session) is not None
        finished.set()
        return {"available": False}

    monkeypatch.setattr(host_module, "workspace_checkpoint", checkpoint)
    admission = asyncio.create_task(host.admit_run(RunRequest("close safely", session)))
    assert await asyncio.to_thread(entered.wait, 5)
    closing = asyncio.create_task(host.close())
    try:
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(closing, 5)
        assert finished.is_set()
        assert admission.cancelled()
        assert not host._active_tasks
        assert not host._admissions
    finally:
        release.set()
        await closing


async def test_admission_is_one_database_transaction(isolated_workspace, monkeypatch):
    host = host_for(isolated_workspace)
    session = host.create_session()
    original = host.store._append_fact_locked

    def fail_message(*args, **kwargs):
        if args[1] == "message.user":
            raise ValueError("injected database failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(host.store, "_append_fact_locked", fail_message)
    try:
        with pytest.raises(ValueError, match="database failure"):
            await host.admit_run(RunRequest("must commit together", session, "rollback"))
        assert host.store.get_run_identity("rollback") is None
        assert host.store.session_events(session) == []
        assert host.store._connection.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
        assert not host._admissions
    finally:
        await host.close()


async def test_crash_after_acceptance_keeps_goal_without_automatic_execution(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    state = isolated_workspace / "state"
    code = """
import asyncio, json, os, sys
from pathlib import Path
from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.models import RunRequest
async def main():
    settings = Settings(Path(sys.argv[1]), Path(sys.argv[2]), 'scripted', None, None, 'saved-model')
    host = RuntimeHost(settings, enable_mcp=False)
    session = host.create_session()
    run = await host.admit_run(RunRequest('survive acceptance crash', session, mode='plan'))
    print(json.dumps({'session': session, 'run': run}), flush=True)
    os._exit(0)
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(repo), str(state)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 0, child.stderr
    identities = json.loads(child.stdout)
    provider = ScriptedProvider([{"text": "recovered"}])
    host = RuntimeHost(replace(settings_for(repo), state_dir=state, model="new-default"), provider)
    try:
        assert provider.requests == []
        assert host.store.get_run(identities["run"])["status"] == "interrupted"
        result = await host.continue_session(identities["session"])
        assert result.status == "completed"
        assert provider.requests[0].model == "saved-model"
        assert provider.requests[0].messages[0]["content"] == "survive acceptance crash"
        assert not any(tool["name"] == "bash" for tool in provider.requests[0].tools)
    finally:
        await host.close()


def test_http_202_has_durable_history_and_immediate_stop(isolated_workspace, monkeypatch):
    host = host_for(isolated_workspace)
    gate = threading.Event()

    async def held_execution(*args, **kwargs):
        await asyncio.to_thread(gate.wait, 10)
        raise AssertionError("Execution must be cancelled before this point")

    monkeypatch.setattr(host, "_execute", held_execution)
    try:
        with TestClient(create_app(host)) as client:
            session = client.post(
                "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).json()["session_id"]
            accepted = client.post(
                f"/api/sessions/{session}/runs", json={"prompt": "HTTP durable goal"}
            )
            assert accepted.status_code == 202
            run_id = accepted.json()["run_id"]
            assert client.get(f"/api/runs/{run_id}").status_code == 200
            messages = client.get(f"/api/sessions/{session}/messages").json()
            assert messages == [{"role": "user", "content": "HTTP durable goal"}]
            assert (
                client.post(f"/api/sessions/{session}/runs", json={"prompt": "busy"}).status_code
                == 409
            )
            stopped = client.post(f"/api/runs/{run_id}/cancel")
            assert stopped.status_code == 200
            assert stopped.json()["status"] == "interrupted"
            assert client.get(f"/api/runs/{run_id}/export").text.count('"run.admitted"') == 1
            gate.set()
    finally:
        gate.set()


def test_event_references_protect_attachments_when_used_cache_is_missing(
    isolated_workspace, monkeypatch
):
    host = host_for(isolated_workspace, ScriptedProvider([{"text": "done"}]))
    monkeypatch.setattr(attachments, "mark_attachments_used", lambda *args: None)
    with TestClient(create_app(host)) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        uploaded = client.post(
            f"/api/sessions/{session}/attachments",
            json={
                "name": "notes.txt",
                "content_base64": "bm90ZXM=",
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()
        aid = uploaded["attachment_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={
                "prompt": "read notes",
                "attachment_ids": [aid],
            },
        )
        assert accepted.status_code == 202
        assert client.get(f"/api/sessions/{session}/attachments").json() == []
        used = client.get(f"/api/sessions/{session}/attachments?include_used=true").json()
        assert used[0]["used"] is True
        assert client.delete(
            f"/api/sessions/{session}/attachments/{aid}",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 409
