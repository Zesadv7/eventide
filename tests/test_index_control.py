"""Index management is durable, queued, idempotent and isolated from Agent conversations."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.index_control import IndexController
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.rag.index import IndexStore
from eventide.retrieval_runtime import managed_index_dir
from tests.test_evaluation_control import host_for


def controller_for(tmp_path):
    host = host_for(tmp_path)
    source = Path(host.default_workspace.path)
    (source / ".git/info/exclude").write_text(".eventide/\n")
    (source / "doc.txt").write_text("searchable source code documentation")
    return host, IndexController(host)


async def accept(controller, identity="index_1", **kwargs):
    return await controller.admit(controller.host.default_workspace.project_id, identity,
                                  config_hash=controller.configuration()["config_hash"], **kwargs)


async def test_actual_index_control_and_main_history(tmp_path):
    host, controller = controller_for(tmp_path)
    before = (Path(host.default_workspace.path) / "doc.txt").read_bytes()
    try:
        run = await accept(controller)
        result = await controller.start(run)
        detail = controller.detail(run)
        assert result.status == "completed" and detail["status"] == "completed"
        assert detail["published"]["generation"] == 1
        assert detail["published"]["stats"]["chunks"] == 1
        assert (Path(host.default_workspace.path) / "doc.txt").read_bytes() == before
        assert host.run_execution(run)["kind"] == "index"
        assert controller.list(host.default_workspace.project_id)[0]["run_id"] == run
        assert host.store.usage_events(run_id=run) == []
        with pytest.raises(ValueError, match="ordinary runs|Continue|control|Control"):
            await host.admit_run(RunRequest("use as chat", detail["session_id"]))
    finally:
        await host.close()


async def test_queued_cancel_never_builds_or_charges(tmp_path):
    host, controller = controller_for(tmp_path)
    try:
        for _ in range(4):
            await host.admit_run(RunRequest("read", host.create_session(), mode="plan",
                                            execution_target="snapshot"))
        run = await accept(controller)
        task = controller.start(run)
        assert host.dispatch.snapshot()["queued"][0]["run_id"] == run
        with pytest.raises(ValueError, match="busy"):
            await accept(controller, "index_2")
        await host.cancel_run(run)
        assert task.cancelled() and controller.detail(run)["status"] == "interrupted"
        assert not (Path(host.default_workspace.path) / ".eventide/index.sqlite").exists()
        assert host.store.usage_events(run_id=run) == []
    finally:
        await host.close()


async def test_restart_parks_unstarted_index_and_does_not_offer_continue(tmp_path):
    host, controller = controller_for(tmp_path)
    run = await accept(controller)
    settings = host.settings
    # Simulate process loss of in-memory admission ownership, preserving canonical facts.
    host._release_admission(run)
    host._admission_done.pop(run, None)
    await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        await restored.initialize()
        detail = IndexController(restored).detail(run)
        assert detail["status"] == "interrupted" and detail["published"] is None
        assert not (Path(restored.default_workspace.path) / ".eventide/index.sqlite").exists()
        with pytest.raises(ValueError, match="ordinary runs|Continue|control|Control"):
            await restored.continue_session(detail["session_id"])
    finally:
        await restored.close()


async def test_failed_build_stays_in_history_and_allows_explicit_new_request(tmp_path, monkeypatch):
    host, controller = controller_for(tmp_path)
    async def failed(*args, **kwargs):
        raise RuntimeError("index changed")
    monkeypatch.setattr("eventide.index_control.prepare_index", failed)
    try:
        run = await accept(controller)
        with pytest.raises(RuntimeError, match="index changed"):
            await controller.start(run)
        assert controller.detail(run)["status"] == "failed"
        await asyncio.sleep(0)
        assert await accept(controller, "index_2") == "index_2"
    finally:
        await host.close()


def test_http_idempotency_origin_drift_and_restart_replay(tmp_path):
    host, controller = controller_for(tmp_path)
    settings = host.settings
    project = host.default_workspace.project_id
    body = {"config_hash": controller.configuration()["config_hash"]}
    endpoint = f"/api/projects/{project}/indexes"
    with TestClient(create_app(host)) as client:
        assert client.post(endpoint, json=body).status_code == 422
        assert client.post(endpoint, json=body, headers={"Idempotency-Key": "foreign",
                                                       "Origin": "https://foreign.invalid"}
                           ).status_code == 403
        stale = client.post(endpoint, json={"config_hash": "0" * 64},
                            headers={"Idempotency-Key": "stale"})
        assert stale.status_code == 409
        assert stale.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        response = client.post(endpoint, json=body, headers={"Idempotency-Key": "build"})
        assert response.status_code == 202, response.text
        receipt = response.json()
        async def wait():
            task = host._run_tasks.get(receipt["run_id"])
            if task:
                await task
        client.portal.call(wait)
        assert client.get(receipt["status_url"]).json()["published"]["generation"] == 1
        again = client.post(endpoint, json=body, headers={"Idempotency-Key": "build"})
        assert again.json() == receipt
        conflict = client.post(endpoint, json={**body, "force": True},
                               headers={"Idempotency-Key": "build"})
        assert conflict.status_code == 409
        stopped = client.post(receipt["status_url"] + "/cancel",
                              headers={"Idempotency-Key": "stop"})
        assert stopped.json()["status"] == "completed"
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    with TestClient(create_app(restored)) as client:
        assert client.post(endpoint, json=body,
                           headers={"Idempotency-Key": "build"}).json() == receipt
        assert len(client.get(endpoint).json()) == 1
        index = IndexStore(Path(restored.default_workspace.path),
                           index_dir=managed_index_dir(restored, restored.default_workspace))
        assert index.generation() == 1


def test_http_queue_cancel_is_idempotent_and_keeps_agent_roots(tmp_path):
    host, controller = controller_for(tmp_path)
    project = host.default_workspace.project_id
    with TestClient(create_app(host)) as client:
        async def occupy():
            for _ in range(4):
                await host.admit_run(RunRequest("read", host.create_session(), mode="plan",
                                                execution_target="snapshot"))
        client.portal.call(occupy)
        body = {"config_hash": client.get("/api/indexes/configuration").json()["config_hash"]}
        receipt = client.post(f"/api/projects/{project}/indexes", json=body,
                              headers={"Idempotency-Key": "queued"}).json()
        path = receipt["status_url"] + "/cancel"
        assert client.get(receipt["status_url"]).json()["status"] == "queued"
        first = client.post(path, headers={"Idempotency-Key": "stop"})
        assert first.status_code == 200 and first.json()["status"] == "interrupted"
        assert client.post(path, headers={"Idempotency-Key": "stop"}).json() == first.json()
        assert host.dispatch.snapshot()["counts"]["active_roots"] == 4
        markers = [e for e in host.store.run_events(receipt["run_id"])
                   if e["type"] == "index.cancel_requested"]
        assert len(markers) == 1
        assert client.get("/api/indexes/missing").status_code == 404
        assert client.get("/api/projects/missing/indexes").status_code == 404
        assert client.post("/api/indexes/missing/cancel",
                           headers={"Idempotency-Key": "none"}).status_code == 404
        assert client.post("/api/projects/missing/indexes", json=body,
                           headers={"Idempotency-Key": "none"}).status_code == 404


async def test_control_remote_build_charges_exact_project_and_no_generation(tmp_path, monkeypatch):
    from dataclasses import replace

    from eventide import embedding_runtime
    from eventide.usage import build_usage_ledger
    from tests.test_embedding_runtime import Client
    from tests.test_index_runtime import api_response

    host, controller = controller_for(tmp_path)
    host.settings = replace(host.settings, embedding_provider="openai_compatible",
                            embedding_model="remote-fixture", embedding_api_key="fixture-secret")
    client = Client(api_response(1))
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    try:
        run = await accept(controller)
        await controller.start(run)
        ledger = build_usage_ledger(host.store.usage_events(run_id=run))
        assert ledger["attempt_count"] == 1 and ledger["reported_totals"] == {"input_tokens": 3}
        assert ledger["calls"][0]["kind"] == "embedding"
        reservation = next(e["payload"] for e in host.store.run_events(run)
                           if e["type"] == "budget.reserved")
        assert {"kind": "project", "identity": host.default_workspace.project_id} in (
            reservation["scopes"]
        )
        assert not host.provider.requests and client.closed
        assert "fixture-secret" not in str(host.store.run_events(run))
    finally:
        await host.close()
