"""Reviewed history imports bind immutable content and commit the receipt with data."""

import asyncio
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.history_import_control import HistoryImportController
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_delivery_runtime import delivery_host
from tests.test_legacy_migration import legacy_database


async def preview(controller, source, run="preview"):
    project = controller.host.default_workspace.project_id
    await controller.admit(project, run, action="preview", source=str(source))
    await controller.start(run)
    return controller.detail(run)["preview"]["sha256"]


async def accept_import(controller, sha, run="apply"):
    await controller.admit(controller.host.default_workspace.project_id, run,
                           action="import", preview_id="preview", archive_sha256=sha)


async def test_snapshot_is_frozen_and_import_survives_restart(tmp_path):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    settings = host.settings
    controller = HistoryImportController(host)
    sha = await preview(controller, source)
    assert controller.detail("preview")["preview"]["counts"]["sessions"] == 1
    # Source changes after preview cannot silently alter reviewed content.
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE runs SET output='changed after preview'")
    await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    controller = HistoryImportController(restored)
    try:
        await accept_import(controller, sha)
        await controller.start("apply")
        detail = controller.detail("apply")
        assert detail["status"] == "completed" and detail["result"]["sessions"] == 1
        sessions = restored.store.list_sessions(restored.default_workspace.workspace_id,
                                                include_archived=True)
        imported = next(s for s in sessions if s["id"].startswith("legacy_"))
        run = restored.store.list_runs(imported["id"])[0]
        assert restored.store.get_run(run["id"])["output"] == "done"
        assert restored.store.usage_events(session_id=imported["id"]) == []
        await accept_import(controller, sha, "duplicate")
        with pytest.raises(ValueError, match="already"):
            await controller.start("duplicate")
        assert controller.detail("duplicate")["status"] == "failed"
        assert restored.store.get_provider_config() is None
    finally:
        await restored.close()


@pytest.mark.parametrize("tamper", ["bytes", "symlink"])
async def test_snapshot_tampering_never_imports(tmp_path, tamper):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    controller = HistoryImportController(host)
    try:
        sha = await preview(controller, source)
        path = controller._path("preview")
        if tamper == "bytes":
            path.write_text("{}")
        else:
            path.unlink()
            path.symlink_to(source)
        await accept_import(controller, sha)
        with pytest.raises(ValueError, match="changed"):
            await controller.start("apply")
        assert not any(s["id"].startswith("legacy_") for s in host.store.list_sessions(
            host.default_workspace.workspace_id, include_archived=True))
    finally:
        await host.close()


async def test_import_and_terminal_receipt_rollback_together(tmp_path, monkeypatch):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    controller = HistoryImportController(host)
    try:
        sha = await preview(controller, source)
        await accept_import(controller, sha)
        original = host.store._append_fact_locked
        def fail(session, kind, payload, **kwargs):
            if kind == "run.completed" and kwargs.get("run_id") == "apply":
                raise RuntimeError("completion failure")
            return original(session, kind, payload, **kwargs)
        monkeypatch.setattr(host.store, "_append_fact_locked", fail)
        with pytest.raises(RuntimeError, match="completion"):
            await controller.start("apply")
        assert controller.detail("apply")["result"] is None
        assert not any(s["id"].startswith("legacy_") for s in host.store.list_sessions(
            host.default_workspace.workspace_id, include_archived=True))
    finally:
        await host.close()


async def test_cancel_waits_for_import_commit_and_keeps_completed_result(tmp_path, monkeypatch):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    controller = HistoryImportController(host)
    entered, finish = threading.Event(), threading.Event()
    original = controller._import
    def delayed(*args):
        entered.set()
        assert finish.wait(5)
        return original(*args)
    monkeypatch.setattr(controller, "_import", delayed)
    try:
        sha = await preview(controller, source)
        await accept_import(controller, sha)
        task = controller.start("apply")
        assert await asyncio.to_thread(entered.wait, 5)
        cancel = asyncio.create_task(host.cancel_run("apply"))
        await asyncio.sleep(.02)
        assert not cancel.done()
        finish.set()
        await cancel
        assert task.cancelled()
        assert controller.detail("apply")["status"] == "completed"
        assert controller.detail("apply")["result"]["sessions"] == 1
    finally:
        finish.set()
        await host.close()


async def test_queued_preview_cancel_has_no_archive_or_import(tmp_path):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    controller = HistoryImportController(host)
    try:
        for _ in range(4):
            await host.admit_run(RunRequest("read", host.create_session(), mode="plan",
                                            execution_target="snapshot"))
        await controller.admit(host.default_workspace.project_id, "queued", action="preview",
                               source=str(source))
        task = controller.start("queued")
        await host.cancel_run("queued")
        assert task.cancelled()
        assert not controller._path("queued").exists()
        assert controller.detail("queued")["status"] == "interrupted"
    finally:
        await host.close()


def test_http_review_confirmation_and_replay(tmp_path):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    path = f"/api/projects/{host.default_workspace.project_id}/history-imports"
    with TestClient(create_app(host)) as client:
        body = {"source": str(source)}
        assert client.post(path + "/preview", json=body).status_code == 422
        response = client.post(path + "/preview", json=body,
                               headers={"Idempotency-Key": "preview"})
        assert response.status_code == 202, response.text
        receipt = response.json()
        async def wait(run):
            task = host._run_tasks.get(run)
            if task:
                await task
        client.portal.call(wait, receipt["run_id"])
        detail = client.get(receipt["status_url"]).json()
        assert detail["preview"]["counts"]["sessions"] == 1
        assert str(source) not in json.dumps(host.store.run_events(receipt["run_id"]))
        apply = {"preview_id": receipt["run_id"],
                 "archive_sha256": detail["preview"]["sha256"]}
        assert client.post(path, json=apply,
                           headers={"Idempotency-Key": "apply"}).status_code == 422
        apply["confirmed"] = True
        result = client.post(path, json=apply, headers={"Idempotency-Key": "apply"})
        assert result.status_code == 202, result.text
        imported = result.json()
        client.portal.call(wait, imported["run_id"])
        assert client.get(imported["status_url"]).json()["result"]["sessions"] == 1
        assert client.post(path, json=apply,
                           headers={"Idempotency-Key": "apply"}).json() == imported
        assert len(client.get(path).json()) == 2


def test_imported_message_identity_pagination_and_strict_confirmation(tmp_path):
    source = tmp_path / "legacy.db"
    connection = legacy_database(source)
    for seq in range(2, 107):
        connection.execute("INSERT INTO messages VALUES('old-session',?,'assistant',?)",
                           (seq, json.dumps("message " + str(seq))))
    connection.commit()
    connection.close()
    host = delivery_host(tmp_path)
    from eventide.migration import import_v02_database
    import_v02_database(host.store, source, host.default_workspace.workspace_id)
    with TestClient(create_app(host)) as client:
        path = "/api/sessions/old-session/legacy-messages"
        recent = client.get(path).json()
        assert len(recent) == 50
        older = client.get(path, params={"before": recent[0]["seq"], "limit": 100}).json()
        assert len(older) == 56
        assert older[0]["content"] == "hello"
        assert older[-1]["seq"] < recent[0]["seq"]
        assert len({m["seq"] for m in recent + older}) == 106
        assert client.get(path, params={"limit": 101}).status_code == 422
        assert client.get("/api/sessions/missing/legacy-messages").status_code == 404
        plain = host.create_session()
        assert client.get(f"/api/sessions/{plain}/legacy-messages").status_code == 404
        apply = {"preview_id": "preview", "archive_sha256": "a" * 64}
        for invalid in [False, 1, "true"]:
            assert client.post(
                f"/api/projects/{host.default_workspace.project_id}/history-imports",
                json={**apply, "confirmed": invalid},
                headers={"Idempotency-Key": "invalid"},
            ).status_code == 422


async def test_restart_parks_unstarted_preview_and_cannot_continue(tmp_path):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    controller = HistoryImportController(host)
    await controller.admit(host.default_workspace.project_id, "crashed", action="preview",
                           source=str(source))
    settings = host.settings
    host._release_admission("crashed")
    host._admission_done.pop("crashed", None)
    await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        await restored.initialize()
        controller = HistoryImportController(restored)
        detail = controller.detail("crashed")
        assert detail["status"] == "interrupted"
        assert not controller._path("crashed").exists()
        with pytest.raises(ValueError, match="Control|control|ordinary"):
            await restored.continue_session(detail["session_id"])
    finally:
        await restored.close()


def test_http_origin_missing_history_and_cancel_queued_preview(tmp_path):
    source = tmp_path / "legacy.db"
    legacy_database(source).close()
    host = delivery_host(tmp_path)
    path = f"/api/projects/{host.default_workspace.project_id}/history-imports"
    with TestClient(create_app(host)) as client:
        async def fill():
            for _ in range(4):
                await host.admit_run(RunRequest("read", host.create_session(), mode="plan",
                                                execution_target="snapshot"))
        client.portal.call(fill)
        body = {"source": str(source)}
        assert client.post(path + "/preview", json=body, headers={
            "Idempotency-Key": "foreign", "Origin": "https://foreign.invalid",
        }).status_code == 403
        receipt = client.post(path + "/preview", json=body,
                              headers={"Idempotency-Key": "queued"}).json()
        stop = receipt["status_url"] + "/cancel"
        result = client.post(stop, headers={"Idempotency-Key": "stop"})
        assert result.json()["status"] == "interrupted"
        assert client.post(stop, headers={"Idempotency-Key": "stop"}).json() == result.json()
        assert client.get("/api/history-imports/missing").status_code == 404
        assert client.get("/api/projects/missing/history-imports").status_code == 404
        assert client.post("/api/history-imports/missing/cancel",
                           headers={"Idempotency-Key": "missing"}).status_code == 404


async def test_empty_preview_cannot_be_imported_twice_or_into_another_project(tmp_path):
    source = tmp_path / "legacy.db"
    connection = legacy_database(source)
    for table in ("messages", "events", "runs", "sessions"):
        connection.execute(f"DELETE FROM {table}")
    connection.commit()
    connection.close()
    host = delivery_host(tmp_path)
    controller = HistoryImportController(host)
    try:
        sha = await preview(controller, source)
        other = tmp_path / "other"
        other.mkdir()
        workspace = host.resolve_or_register_workspace(other)
        with pytest.raises(ValueError, match="project"):
            await controller.admit(workspace.project_id, "foreign", action="import",
                                   preview_id="preview", archive_sha256=sha)
        await accept_import(controller, sha)
        await controller.start("apply")
        assert controller.detail("preview")["imported"]
        await accept_import(controller, sha, "repeat")
        with pytest.raises(ValueError, match="already imported"):
            await controller.start("repeat")
        assert controller.detail("repeat")["result"] is None
    finally:
        await host.close()
