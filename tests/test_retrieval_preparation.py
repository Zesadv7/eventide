"""Actual execution-copy preparation, frozen options and event-backed availability."""

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.index_control import IndexController
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.rag.index import IndexStore
from eventide.retrieval_runtime import index_status, managed_index_dir
from tests.test_delivery_repair import done, edit
from tests.test_delivery_runtime import CHECK, delivery_host
from tests.test_evaluation_control import host_for
from tests.test_host import make_repo


def search_script():
    return [
        {"tool_calls": [{"id": "search", "name": "search_knowledge",
                         "arguments": {"query": "cancel restart"}}]},
        {"tool_calls": [{"id": "read", "name": "read_file",
                         "arguments": {"path": "behavior.txt"}}]},
        {"text": "Evidence checked: behavior.txt:1"},
    ]


def source_fixture(host):
    root = Path(host.default_workspace.path)
    (root / "behavior.txt").write_text("cancel restart does not replay tools\n")
    subprocess.run(["git", "-C", str(root), "add", "behavior.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    return root


@pytest.mark.parametrize("target", ["source", "worktree"])
async def test_prepare_uses_actual_copy_without_source_artifacts(tmp_path, target):
    host = host_for(tmp_path)
    root = source_fixture(host)
    host.provider = ScriptedProvider(search_script())
    try:
        result = await host.run(RunRequest("investigate", knowledge_index="prepare",
                                          execution_target=target))
        assert result.status == "completed"
        state = host.store.get_run(result.run_id)
        actual = host.default_workspace
        if target == "worktree":
            binding = host._execution_binding(result.run_id)
            actual = host.store.get_workspace(binding["execution_workspace_id"])
        assert state["retrieval"]["workspace_id"] == actual.workspace_id
        assert state["retrieval"]["status"] == "available"
        evidence = state["tools"]["search"]["metadata"]["retrieval"]
        assert evidence["workspace_id"] == actual.workspace_id
        assert evidence["index_generation"] == 1
        assert all(hit["freshness"] == "fresh" for hit in evidence["hits"])
        assert "does not replay" in state["tools"]["read"]["content"]
        assert (managed_index_dir(host, actual) / "index.sqlite").is_file()
        assert not (root / ".eventide").exists() and not (root / ".gitignore").exists()
        assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"]) == b""
        await host.run(RunRequest("again", knowledge_index="prepare",
                                  execution_target=target, session_id=result.session_id))
        latest = host.store.latest_run(result.session_id)
        assert host.store.get_run(latest["id"])["retrieval"]["build"]["rebuilt_files"] == 0
    finally:
        await host.close()


async def test_existing_worktree_does_not_borrow_source_index(tmp_path):
    host = host_for(tmp_path)
    source_fixture(host)
    try:
        await host.run(RunRequest("source", knowledge_index="prepare"))
        result = await host.run(RunRequest("copy", execution_target="worktree"))
        state = host.store.get_run(result.run_id)
        assert state["retrieval"]["status"] == "unavailable"
        assert state["retrieval"]["workspace_id"] != host.default_workspace.workspace_id
        assert "search_knowledge" not in {t["name"] for t in host.provider.requests[-1].tools}
    finally:
        await host.close()


@pytest.mark.parametrize("target,profile", [("snapshot", "local"), ("source", "sandbox")])
async def test_unsupported_prepare_rejected_before_copy_creation(tmp_path, target, profile):
    host = host_for(tmp_path)
    try:
        with pytest.raises(ValueError, match="unavailable"):
            await host.admit_run(RunRequest("read", mode="plan", knowledge_index="prepare",
                                           execution_target=target, execution_profile=profile))
        sessions = host.store.list_sessions(host.default_workspace.workspace_id)
        assert all(not host.store.list_runs(session["id"]) for session in sessions)
    finally:
        await host.close()


async def test_nested_state_refused_and_config_drift_not_silently_rebuilt(tmp_path):
    (tmp_path / "inside").mkdir()
    (tmp_path / "outside").mkdir()
    inside = host_for(tmp_path / "inside", inside=True)
    try:
        with pytest.raises(ValueError, match="outside source"):
            await inside.admit_run(RunRequest("read", knowledge_index="prepare"))
    finally:
        await inside.close()
    host = host_for(tmp_path / "outside")
    source_fixture(host)
    try:
        run = await host.admit_run(RunRequest("read", knowledge_index="prepare"))
        host.settings = replace(host.settings, embedding_dim=64)
        result = await host.start_admitted_run(run)
        assert result.status == "failed"
        assert host.store.get_run(run)["retrieval"]["status"] == "failed"
        assert not host.provider.requests
    finally:
        await host.close()


async def test_prepare_failure_does_not_enter_model_loop(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    source_fixture(host)
    async def fail(*args, **kwargs):
        raise OSError("fixture disk failure")
    monkeypatch.setattr("eventide.index_runtime.build_index", fail)
    try:
        result = await host.run(RunRequest("read", knowledge_index="prepare"))
        assert result.status == "failed" and not host.provider.requests
        state = host.store.get_run(result.run_id)
        assert state["retrieval"]["status"] == "failed"
        assert "disk failure" in state["retrieval"]["detail"]
    finally:
        await host.close()


async def test_cancel_waiting_for_shared_index_lock_never_calls_model(tmp_path):
    host = host_for(tmp_path)
    source_fixture(host)
    try:
        async with host._index_build_locks[host.default_workspace.workspace_id]:
            run = await host.admit_run(RunRequest("read", knowledge_index="prepare"))
            task = host.start_admitted_run(run)
            for _ in range(100):
                if host.store.get_run(run)["retrieval"]:
                    break
                await asyncio.sleep(.01)
            assert host.store.get_run(run)["retrieval"]["status"] == "preparing"
            await host.cancel_run(run)
            assert task.done()
        assert not host.provider.requests
        assert host.store.get_run(run)["retrieval"]["reason"] == "cancelled"
        assert host.store.usage_events(run_id=run) == []
    finally:
        await host.close()


async def test_legacy_readonly_managed_precedence_and_no_corrupt_fallback(tmp_path):
    host = host_for(tmp_path)
    root = source_fixture(host)
    IndexStore(root).build()
    old = (root / ".eventide/index.sqlite").read_bytes()
    try:
        assert index_status(host, host.default_workspace)["location"] == "legacy"
        await host.run(RunRequest("prepare", knowledge_index="prepare"))
        assert (root / ".eventide/index.sqlite").read_bytes() == old
        assert index_status(host, host.default_workspace)["location"] == "managed"
        (managed_index_dir(host, host.default_workspace) / "index.sqlite").write_bytes(b"broken")
        status = index_status(host, host.default_workspace)
        assert status["status"] == "unavailable" and status["location"] == "managed"
    finally:
        await host.close()


async def test_restart_does_not_replay_admitted_preparation(tmp_path):
    host = host_for(tmp_path)
    source_fixture(host)
    run = await host.admit_run(RunRequest("prepare", knowledge_index="prepare"))
    settings = host.settings
    host._release_admission(run)
    await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        await restored.initialize()
        assert restored.store.get_run(run)["status"] == "interrupted"
        assert not restored.provider.requests
        assert not managed_index_dir(restored, restored.default_workspace).exists()
    finally:
        await restored.close()


async def test_every_delivery_writer_prepares_its_own_candidate(tmp_path):
    host = delivery_host(tmp_path, [edit("price + fraction", "price - fraction"), done(),
                                    edit("price - fraction", "price * (1 - fraction)"), done()])
    try:
        prepared = await host.manage_delivery("prepare", name="indexed")
        result = await host.manage_delivery("repair", delivery_id=prepared["delivery_id"],
                                           goal="fix", checks=[CHECK], knowledge_index="prepare")
        assert result["reason"] == "passed" and len(result["rounds"]) == 2
        statuses = [host.store.get_run(r["worker_run_id"])["retrieval"] for r in result["rounds"]]
        assert all(s["status"] == "available" for s in statuses)
        assert statuses[0]["workspace_id"] == statuses[1]["workspace_id"]
        assert statuses[0]["workspace_id"] != host.default_workspace.workspace_id
        assert statuses[1]["generation"] == 2
        assert statuses[1]["build"]["rebuilt_files"] == 1
        assert not (Path(host.default_workspace.path) / ".eventide").exists()
    finally:
        await host.close()


def test_index_api_explicit_workspace_and_cross_project_rejection(tmp_path):
    host = host_for(tmp_path)
    source_fixture(host)
    other = host.resolve_or_register_workspace(make_repo(tmp_path / "other"))
    config = IndexController(host).configuration()
    project = host.default_workspace.project_id
    with TestClient(create_app(host)) as client:
        bad = client.post(f"/api/projects/{project}/indexes", json={
            "workspace_id": other.workspace_id, "config_hash": config["config_hash"],
        }, headers={"Idempotency-Key": "other"})
        assert bad.status_code == 404
        status = client.get(f"/api/workspaces/{host.default_workspace.workspace_id}/index-status")
        assert status.status_code == 200 and status.json()["status"] == "unavailable"


@pytest.mark.parametrize("target", ["source", "worktree"])
async def test_continue_inherits_preparation_and_refuses_changed_configuration(tmp_path, target):
    host = host_for(tmp_path)
    source_fixture(host)
    host.settings = replace(host.settings, max_steps=1)
    host.provider = ScriptedProvider(search_script())
    try:
        first = await host.run(RunRequest("investigate", knowledge_index="prepare",
                                          execution_target=target))
        assert first.status == "interrupted"
        admitted = host._admitted_intent(first.run_id)
        original = host.settings
        host.settings = replace(original, embedding_dim=64)
        with pytest.raises(ValueError, match="Continue refused"):
            await host.continue_session(first.session_id)
        assert len(host.provider.requests) == 1
        host.settings = original
        host.provider = ScriptedProvider([{"text": "continued"}])
        second = await host.continue_session(first.session_id)
        assert second.status == "completed"
        continued = host._admitted_intent(second.run_id)
        assert continued["knowledge_index"] == "prepare"
        assert continued["retrieval_configuration"] == admitted["retrieval_configuration"]
        state = host.store.get_run(second.run_id)
        assert state["retrieval"]["build"]["rebuilt_files"] == 0
        assert state["retrieval"]["workspace_id"] == host.store.get_run(
            first.run_id
        )["retrieval"]["workspace_id"]
    finally:
        await host.close()


async def test_agent_sees_stale_diagnostic_then_reads_current_source(tmp_path):
    host = host_for(tmp_path)
    root = source_fixture(host)
    try:
        await host.run(RunRequest("prepare", knowledge_index="prepare"))
        (root / "behavior.txt").write_text("cancel restart now parks for user confirmation\n")
        host.provider = ScriptedProvider(search_script())
        result = await host.run(RunRequest("verify current behavior", knowledge_index="existing"))
        assert result.status == "completed"
        state = host.store.get_run(result.run_id)
        search = state["tools"]["search"]
        assert "does not replay" not in search["content"]
        assert search["metadata"]["retrieval"]["hits"][0]["freshness"] == "changed"
        assert "user confirmation" in state["tools"]["read"]["content"]
        assert "does not replay" not in str(host.provider.requests[1].messages)
    finally:
        await host.close()


@pytest.mark.parametrize("copy_first", [False, True])
def test_manual_index_key_cannot_change_target_workspace(tmp_path, copy_first):
    host = host_for(tmp_path)
    root = source_fixture(host)
    copy = tmp_path / "copy"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(copy)],
                   check=True, capture_output=True)
    workspace = host.resolve_or_register_workspace(copy)
    targets = [host.default_workspace.workspace_id, workspace.workspace_id]
    if copy_first:
        targets.reverse()
    project = host.default_workspace.project_id
    config = IndexController(host).configuration()
    with TestClient(create_app(host)) as client:
        body = {"config_hash": config["config_hash"], "workspace_id": targets[0]}
        endpoint = f"/api/projects/{project}/indexes"
        first = client.post(endpoint, json=body, headers={"Idempotency-Key": "same-key"})
        assert first.status_code == 202
        replay = client.post(endpoint, json=body, headers={"Idempotency-Key": "same-key"})
        assert replay.status_code == 202 and replay.json() == first.json()
        changed = client.post(endpoint, json={**body, "workspace_id": targets[1]},
                              headers={"Idempotency-Key": "same-key"})
        assert changed.status_code == 409
        assert len(client.get(endpoint).json()) == 1
