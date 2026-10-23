"""Preparation policy is shared by manual, session and delivery paths."""

import asyncio
import json
import subprocess
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.index_control import IndexController
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.rag.index import IndexStore
from eventide.retrieval_runtime import freeze_configuration, validate_frozen_configuration
from tests.test_delivery_repair import done, edit
from tests.test_delivery_runtime import CHECK, delivery_host
from tests.test_evaluation_control import host_for
from tests.test_retrieval_preparation import search_script, source_fixture


@pytest.mark.parametrize("target", ["source", "worktree"])
async def test_manual_and_task_prepare_keep_selected_policy(tmp_path, target):
    host = host_for(tmp_path)
    root = source_fixture(host)
    (root / "logic.py").write_text("def cancel_worker():\n    return 'cancel restart'\n")
    (root / "uv.lock").write_text("excluded_lock_marker = 'cancel restart'\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "policy fixture"],
                   check=True, capture_output=True)
    controller = IndexController(host)
    try:
        if target == "source":
            await controller.admit(host.default_workspace.project_id, "manual-policy",
                                   config_hash=controller.configuration()["config_hash"],
                                   code_structure=True, source_scope="all")
            await controller.start("manual-policy")
        host.provider = ScriptedProvider(search_script())
        result = await host.run(RunRequest(
            "investigate", knowledge_index="prepare", execution_target=target,
            code_structure=True, source_scope="all",
        ))
        assert result.status == "completed"
        admitted = host._admitted_intent(result.run_id)
        assert admitted["code_structure"] is True and admitted["source_scope"] == "all"
        policy = admitted["index_configuration"]
        assert json.loads(policy["chunking_profile"])["code_structure"] is True
        build = host.store.get_run(result.run_id)["retrieval"]["build"]
        assert build["source_scope"] == "all"
        assert build["structured_files"] >= 1
        if target == "source":
            assert build["rebuilt_files"] == 0
        assert not (root / ".eventide").exists()
    finally:
        await host.close()


async def test_continue_inherits_scope_and_structure_without_rebuilding(tmp_path):
    host = host_for(tmp_path)
    source_fixture(host)
    host.settings = replace(host.settings, max_steps=1)
    host.provider = ScriptedProvider(search_script())
    try:
        first = await host.run(RunRequest("inspect", knowledge_index="prepare",
                                         code_structure=True, source_scope="all"))
        assert first.status == "interrupted"
        original = host._admitted_intent(first.run_id)
        host.provider = ScriptedProvider([{"text": "continued"}])
        second = await host.continue_session(first.session_id)
        assert second.status == "completed"
        inherited = host._admitted_intent(second.run_id)
        for name in ("code_structure", "source_scope", "index_configuration"):
            assert inherited[name] == original[name]
        assert host.store.get_run(second.run_id)["retrieval"]["build"]["rebuilt_files"] == 0
    finally:
        await host.close()


async def test_queued_preparation_rejects_changed_rules_without_model(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    source_fixture(host)
    try:
        run = await host.admit_run(RunRequest("inspect", knowledge_index="prepare"))
        monkeypatch.setattr("eventide.retrieval_runtime.source_selection_identity",
                            lambda scope: "different selection rules")
        result = await host.start_admitted_run(run)
        assert result.status == "failed" and not host.provider.requests
        assert "rules changed" in host.store.get_run(run)["retrieval"]["detail"]
    finally:
        await host.close()


async def test_manual_queued_preparation_also_rejects_rule_changes(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    source_fixture(host)
    controller = IndexController(host)
    try:
        await controller.admit(host.default_workspace.project_id, "manual",
                               config_hash=controller.configuration()["config_hash"])
        monkeypatch.setattr("eventide.retrieval_runtime.source_selection_identity",
                            lambda scope: "different selection rules")
        with pytest.raises(ValueError, match="rules changed"):
            await controller.start("manual")
        assert controller.detail("manual")["status"] == "failed"
        assert not host.provider.requests
    finally:
        await host.close()


async def test_writer_rounds_inherit_the_same_policy(tmp_path):
    host = delivery_host(tmp_path, [edit("price + fraction", "price - fraction"), done(),
                                    edit("price - fraction", "price * (1 - fraction)"), done()])
    try:
        prepared = await host.manage_delivery("prepare", name="structured")
        result = await host.manage_delivery("repair", delivery_id=prepared["delivery_id"],
                                           goal="fix", checks=[CHECK], knowledge_index="prepare",
                                           code_structure=True, source_scope="all")
        assert result["reason"] == "passed" and len(result["rounds"]) == 2
        for row in result["rounds"]:
            intent = host._admitted_intent(row["worker_run_id"])
            assert intent["code_structure"] is True and intent["source_scope"] == "all"
            build = host.store.get_run(row["worker_run_id"])["retrieval"]["build"]
            assert build["structured_files"] >= 1
            assert build["source_scope"] == "all"
    finally:
        await host.close()


def test_http_policy_participates_in_idempotency_and_legacy_defaults(tmp_path):
    host = host_for(tmp_path)
    source_fixture(host)
    session = host.create_session()
    with TestClient(create_app(host)) as client:
        endpoint = f"/api/sessions/{session}/runs"
        body = {"prompt": "inspect", "knowledge_index": "prepare",
                "code_structure": True, "source_scope": "all"}
        first = client.post(endpoint, json=body, headers={"Idempotency-Key": "policy"})
        assert first.status_code == 202
        repeat = client.post(endpoint, json=body, headers={"Idempotency-Key": "policy"})
        assert repeat.status_code == 202 and repeat.json() == first.json()
        for change in ({"source_scope": "code"}, {"code_structure": False}):
            changed = client.post(endpoint, json={**body, **change},
                                  headers={"Idempotency-Key": "policy"})
            assert changed.status_code == 409


async def test_historical_existing_does_not_invent_preparation_policy(tmp_path):
    host = host_for(tmp_path)
    try:
        current = freeze_configuration(host.settings, "existing")
        legacy = {name: current[name] for name in ("knowledge_index", "retrieval_configuration")}
        assert validate_frozen_configuration(host.settings, legacy) == legacy
        legacy["knowledge_index"] = "prepare"
        with pytest.raises(ValueError, match="not recorded"):
            validate_frozen_configuration(host.settings, legacy)
    finally:
        await host.close()

@pytest.mark.parametrize("manual", [False, True])
async def test_configuration_drift_while_waiting_for_build_lock_is_rejected(tmp_path, monkeypatch,
                                                                          manual):
    host = host_for(tmp_path)
    source_fixture(host)
    controller = IndexController(host)
    lock = host._index_build_locks[host.default_workspace.workspace_id]
    await lock.acquire()
    try:
        if manual:
            run_id = await controller.admit(host.default_workspace.project_id, "waiting-policy",
                                            config_hash=controller.configuration()["config_hash"])
            task = controller.start(run_id)
        else:
            run_id = await host.admit_run(RunRequest("inspect", knowledge_index="prepare"))
            task = host.start_admitted_run(run_id)
        deadline = time.monotonic() + 5
        while not lock._waiters:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        monkeypatch.setattr("eventide.retrieval_runtime.source_selection_identity",
                            lambda scope: "changed while queued")
        lock.release()
        if manual:
            with pytest.raises(ValueError, match="rules changed"):
                await task
        else:
            assert (await task).status == "failed"
        assert not host.provider.requests
        assert not (host.state_dir / "indexes" / host.default_workspace.workspace_id
                    / "index.sqlite").exists()
    finally:
        if lock.locked():
            lock.release()
        await host.close()

async def test_legacy_policy_change_requires_force_before_creating_managed_index(tmp_path):
    host = host_for(tmp_path)
    root = source_fixture(host)
    legacy = IndexStore(root)
    legacy.build(code_structure=True, source_scope="all")
    legacy_bytes = legacy.path.read_bytes()
    controller = IndexController(host)
    try:
        result = await host.run(RunRequest("inspect", knowledge_index="prepare"))
        assert result.status == "failed" and not host.provider.requests
        managed = host.state_dir / "indexes" / host.default_workspace.workspace_id / "index.sqlite"
        assert not managed.exists()
        await controller.admit(host.default_workspace.project_id, "force-legacy",
                               config_hash=controller.configuration()["config_hash"], force=True)
        await controller.start("force-legacy")
        assert managed.exists() and legacy.path.read_bytes() == legacy_bytes
    finally:
        await host.close()
