"""Worktree management uses one durable control Run and protects existing copies."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.worktree_control import WorktreeController
from tests.test_delivery_runtime import delivery_host


async def admit(controller, run_id, action="create", name="example"):
    project = controller.host.default_workspace.project_id
    catalog = await controller.catalog(project)
    return await controller.admit(project, run_id, action=action, name=name,
                                  expected_catalog_hash=catalog["catalog_hash"])


async def test_create_keep_remove_and_history_protection(tmp_path):
    host = delivery_host(tmp_path)
    controller = WorktreeController(host)
    project = host.default_workspace.project_id
    try:
        await admit(controller, "create")
        result = await controller.start("create")
        assert result.status == "completed"
        detail = controller.detail("create")
        checkout = Path(detail["result"]["path"])
        assert checkout.is_dir()
        assert host.run_execution("create")["operation_id"] == "create"
        assert len(host.store.list_runs(detail["session_id"])) == 1
        assert host.store.usage_events(run_id="create") == []
        await admit(controller, "keep", "keep")
        await controller.start("keep")
        assert (await controller.catalog(project))["worktrees"][0]["kept"]
        await admit(controller, "remove", "remove")
        await controller.start("remove")
        assert not checkout.exists()
        assert len(controller.history(project)) == 3
        await admit(controller, "create_again", name="protected")
        await controller.start("create_again")
        checkout = Path(controller.detail("create_again")["result"]["path"])
        workspace = host.resolve_or_register_workspace(checkout)
        host.create_session(workspace_id=workspace.workspace_id)
        await admit(controller, "protected", "remove", name="protected")
        with pytest.raises(ValueError, match="Session history"):
            await controller.start("protected")
        assert checkout.is_dir()
        assert controller.detail("protected")["status"] == "failed"
        assert (await controller.catalog(project))["worktrees"][0]["history_protected"]
        with pytest.raises(ValueError, match="control|Control|ordinary"):
            await host.admit_run(RunRequest("chat", detail["session_id"]))
    finally:
        await host.close()


async def test_queued_cancel_and_catalog_drift(tmp_path):
    host = delivery_host(tmp_path)
    controller = WorktreeController(host)
    try:
        for _ in range(4):
            await host.admit_run(RunRequest("read", host.create_session(), mode="plan",
                                            execution_target="snapshot"))
        await admit(controller, "queued")
        task = controller.start("queued")
        assert host.dispatch.snapshot()["queued"][0]["run_id"] == "queued"
        await host.cancel_run("queued")
        assert task.cancelled()
        assert controller.detail("queued")["status"] == "interrupted"
        assert not (await controller.catalog(host.default_workspace.project_id))["worktrees"]
    finally:
        await host.close()


async def test_restart_parks_and_never_replays(tmp_path):
    host = delivery_host(tmp_path)
    controller = WorktreeController(host)
    await admit(controller, "crashed")
    host._release_admission("crashed")
    host._admission_done.pop("crashed", None)
    settings = host.settings
    await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        await restored.initialize()
        control = WorktreeController(restored)
        assert control.detail("crashed")["status"] == "interrupted"
        assert not (await control.catalog(restored.default_workspace.project_id))["worktrees"]
        with pytest.raises(ValueError, match="control|Control|ordinary"):
            await restored.continue_session(control.detail("crashed")["session_id"])
    finally:
        await restored.close()


def test_http_receipts_conflicts_and_exact_catalog(tmp_path):
    host = delivery_host(tmp_path)
    project = host.default_workspace.project_id
    path = f"/api/projects/{project}/worktrees"
    with TestClient(create_app(host)) as client:
        catalog = client.get(path).json()
        body = {"action": "create", "name": "http",
                "expected_catalog_hash": catalog["catalog_hash"]}
        assert client.post(path, json=body).status_code == 422
        assert client.post(path, json=body, headers={
            "Idempotency-Key": "create", "Origin": "https://foreign.invalid",
        }).status_code == 403
        response = client.post(path, json=body, headers={"Idempotency-Key": "create"})
        assert response.status_code == 202, response.text
        receipt = response.json()
        async def wait():
            task = host._run_tasks.get(receipt["run_id"])
            if task:
                await task
        client.portal.call(wait)
        detail = client.get(receipt["status_url"]).json()
        assert detail["status"] == "completed", detail
        assert client.post(path, json=body, headers={"Idempotency-Key": "create"}).json() == receipt
        changed = {**body, "name": "other"}
        assert client.post(
            path, json=changed, headers={"Idempotency-Key": "create"},
        ).status_code == 409
        assert client.post(
            path, json=changed, headers={"Idempotency-Key": "stale"},
        ).status_code == 409
        assert len(client.get(path).json()["worktrees"]) == 1
        assert client.post(f'/api/sessions/{detail["session_id"]}/continue').status_code == 409


async def test_admission_freezes_commit_and_rechecks_catalog(tmp_path):
    import subprocess

    host = delivery_host(tmp_path)
    controller = WorktreeController(host)
    project = host.default_workspace.project_id
    try:
        await admit(controller, "fixed")
        base = controller.accepted("fixed")["ref"]
        source = Path(host.default_workspace.path)
        (source / "later.txt").write_text("new source")
        subprocess.run(["git", "-C", str(source), "add", "later.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "later"], check=True,
                       capture_output=True)
        await controller.start("fixed")
        checkout = Path(controller.detail("fixed")["result"]["path"])
        assert not (checkout / "later.txt").exists()
        assert controller.detail("fixed")["result"]["base_commit"] == base
        await admit(controller, "stale_remove", "remove")
        controller.service(project).keep("example")
        with pytest.raises(ValueError, match="catalog changed"):
            await controller.start("stale_remove")
        assert checkout.exists()
        assert controller.detail("stale_remove")["status"] == "failed"
    finally:
        await host.close()


async def test_cancel_waits_for_dispatched_git_result(tmp_path, monkeypatch):
    import threading

    from eventide.worktrees import WorktreeService

    host = delivery_host(tmp_path)
    controller = WorktreeController(host)
    entered, finish = threading.Event(), threading.Event()
    original = WorktreeService.create

    def delayed(service, name, ref):
        entered.set()
        if not finish.wait(5):
            raise RuntimeError("fixture gate timed out")
        return original(service, name, ref)

    monkeypatch.setattr(WorktreeService, "create", delayed)
    try:
        await admit(controller, "stopping")
        task = controller.start("stopping")
        assert await asyncio.to_thread(entered.wait, 5)
        cancelling = asyncio.create_task(host.cancel_run("stopping"))
        await asyncio.sleep(0.02)
        assert not cancelling.done()
        assert host.workspace_busy(host.default_workspace.workspace_id)
        finish.set()
        await cancelling
        assert task.cancelled()
        detail = controller.detail("stopping")
        assert detail["status"] == "completed"
        assert Path(detail["result"]["path"]).is_dir()
        assert not host.workspace_busy(host.default_workspace.workspace_id)
    finally:
        finish.set()
        await host.close()


def test_http_queue_stop_history_and_missing_identity(tmp_path):
    host = delivery_host(tmp_path)
    project = host.default_workspace.project_id
    path = f"/api/projects/{project}/worktrees"
    with TestClient(create_app(host)) as client:
        async def fill():
            for _ in range(4):
                await host.admit_run(RunRequest("read", host.create_session(), mode="plan",
                                                execution_target="snapshot"))
        client.portal.call(fill)
        body = {"action": "create", "name": "queued",
                "expected_catalog_hash": client.get(path).json()["catalog_hash"]}
        receipt = client.post(path, json=body, headers={"Idempotency-Key": "queue"}).json()
        run = receipt["run_id"]
        history = client.get(f"/api/projects/{project}/worktree-operations").json()
        assert history[0]["status"] == "queued"
        assert client.get("/api/worktree-operations/missing").status_code == 404
        assert client.get("/api/projects/missing/worktree-operations").status_code == 404
        assert client.get("/api/projects/missing/worktrees").status_code == 409
        assert client.post("/api/projects/missing/worktrees", json=body,
                           headers={"Idempotency-Key": "missing"}).status_code == 404
        cancel = f"/api/worktree-operations/{run}/cancel"
        assert client.post(cancel).status_code == 422
        cancelled = client.post(cancel, headers={"Idempotency-Key": "stop"})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "interrupted"
        assert client.post(cancel, headers={"Idempotency-Key": "stop"}).json() == cancelled.json()
        assert client.post("/api/worktree-operations/missing/cancel",
                           headers={"Idempotency-Key": "missing"}).status_code == 404
        assert not client.get(path).json()["worktrees"]


async def test_invalid_requests_and_control_identity_never_mutate(tmp_path):
    host = delivery_host(tmp_path)
    controller = WorktreeController(host)
    project = host.default_workspace.project_id
    try:
        version = (await controller.catalog(project))["catalog_hash"]
        for identity, action, name, ref, message in [
            ("bad/id", "create", "safe", "HEAD", "identity"),
            ("bad_action", "unknown", "safe", "HEAD", "action"),
            ("bad_name", "create", "../outside", "HEAD", "name"),
            ("bad_ref", "create", "safe", "--all", "ref"),
            ("missing", "remove", "missing", "HEAD", "not found"),
        ]:
            with pytest.raises(ValueError, match=message):
                await controller.admit(project, identity, action=action, name=name, ref=ref,
                                       expected_catalog_hash=version)
        with pytest.raises(ValueError, match="not found"):
            controller.detail("missing")
        with pytest.raises(ValueError, match="awaiting"):
            controller.start("missing")
        await admit(controller, "owned")
        with pytest.raises(ValueError, match="busy"):
            await admit(controller, "other")
        await controller.start("owned")
        with pytest.raises(ValueError, match="exists"):
            await admit(controller, "duplicate")
        assert len((await controller.catalog(project))["worktrees"]) == 1
    finally:
        await host.close()
