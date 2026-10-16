"""Isolated eval executions share one authority without duplicating attempts."""

import asyncio
from dataclasses import replace

import pytest

from eventide.budgets import BudgetExceeded, BudgetScope
from eventide.eval_evidence import case_evidence
from eventide.evaluation_calls import EvaluationCallBridge
from eventide.evaluation_control import EvaluationController
from eventide.host import RuntimeHost
from eventide.models import ModelRequest, ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_model_budget import admitted
from tests.test_runtime import settings_for


async def authority(tmp_path, provider, *, mode="live", cases=None):
    root = tmp_path / "authority"
    root.mkdir()
    host = RuntimeHost(settings_for(root), provider, enable_mcp=False)
    await host.initialize()
    project = host.default_workspace
    owner = EvaluationController(host).owner(project.project_id)
    intent = {
        "source": "evaluation", "project_id": project.project_id,
        "workspace_id": project.workspace_id,
        "execution_binding": {"kind": "evaluation", "evaluation_id": "evaluation",
                              "workspace_id": project.workspace_id},
    }
    host.store.admit_run("evaluation", owner, [
        ("run.admitted", intent, "system", "runtime"),
        ("evaluation.accepted", {
            "mode": mode, "suite": {"cases": cases},
            "generation": host.generation_settings(host.settings.model),
        }, "system", "runtime"),
    ])
    host.store.append_event("evaluation", "run.started", intent)
    return host


def isolated(tmp_path, main, index=1, **settings):
    root = tmp_path / f"case-{index}"
    root.mkdir()
    host = RuntimeHost(settings_for(root, **settings), ScriptedProvider([]), enable_mcp=False)
    host._evaluation_bridge = EvaluationCallBridge(main, "evaluation", f"case-{index}", index)
    return host


async def test_main_is_sole_attempt_authority_and_local_observed_limit_still_stops(tmp_path):
    provider = ScriptedProvider([
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "file.txt"}}],
         "usage": {"input_tokens": 30, "output_tokens": 5}},
        {"text": "should never dispatch"},
    ])
    main = await authority(tmp_path, provider)
    local = isolated(tmp_path, main, run_token_limit=20)
    (local.settings.workdir / "file.txt").write_text("content")
    try:
        result = await local.run(RunRequest("read"))
        assert result.status == "interrupted"
        assert len(provider.requests) == 1
        assert local.provider.requests == []
        local_ledger = build_usage_ledger(local.store.usage_events(run_id=result.run_id))
        assert local_ledger["attempt_count"] == 0 and not local_ledger["complete"]
        assert local_ledger["authority_references"][0]["run_id"] == "evaluation"
        ledger = build_usage_ledger(local.model_usage_events(result.run_id))
        assert ledger["reported_totals"] == {"input_tokens": 30, "output_tokens": 5}
        assert ledger["attempt_count"] == 1
        assert main.store.facts_by_type(("budget.reserved",))[0]["payload"][
            "context"
        ]["root_run_id"] == "evaluation"
        assert local.budget_status()["reservations"] == []
        assert local._evaluation_bridge is not None
        evidence = local._evaluation_bridge.evidence(tmp_path)
        assert evidence["usage"] == ledger
        with pytest.raises(ValueError, match="authoritative evidence"):
            case_evidence(local.store, tmp_path, main_run_id=result.run_id)
        report = case_evidence(local.store, tmp_path, main_run_id=result.run_id,
                               model_authority=local._evaluation_bridge)
        assert report["usage"] == ledger
        assert report["model_authority"]["database"] == str(main.store.path)
        assert report["database"] == str(local.store.path)
        assert report["main_run_id"] == result.run_id
        assert evidence["model_attempts"][0]["evaluation_origin"]["local_run_id"] == result.run_id
    finally:
        await local.close()
        await main.close()


async def test_project_limit_blocks_isolated_case_before_dispatch(tmp_path):
    provider = ScriptedProvider([{"text": "must not send"}])
    main = await authority(tmp_path, provider)
    local = isolated(tmp_path, main)
    main.configure_budget(
        BudgetScope("project", main.default_workspace.project_id),
        token_limit=0, request_id="block",
    )
    try:
        run = admitted(local)
        with pytest.raises(BudgetExceeded):
            await local._complete_with_retry(ModelRequest("", [], [], "scripted"), run, None)
        assert provider.requests == []
        assert main.budget_status()["reservations"] == []
        assert not local._evaluation_bridge.events()
    finally:
        await local.close()
        await main.close()


async def test_cases_share_two_slots_and_cancelled_unknown_attempt_remains_reserved(tmp_path):
    class Provider:
        def __init__(self):
            self.active = 0
            self.maximum = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, request):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 2:
                self.started.set()
            try:
                await self.release.wait()
                return ModelResponse(text="done", usage={"input_tokens": 1, "output_tokens": 1})
            finally:
                self.active -= 1

    provider = Provider()
    main = await authority(tmp_path, provider)
    cases = [isolated(tmp_path, main, index) for index in (1, 2, 3)]
    tasks = []
    try:
        for case in cases:
            run = admitted(case)
            tasks.append(asyncio.create_task(case._complete_with_retry(
                ModelRequest("", [], [], "scripted", 10), run, None,
            )))
        await asyncio.wait_for(provider.started.wait(), 5)
        assert provider.maximum == 2
        assert len(main.budget_status()["reservations"]) == 2
        tasks[2].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[2]
        assert cases[2]._evaluation_bridge.events() == []
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        provider.release.set()
        await tasks[1]
        ledger = build_usage_ledger(main.store.usage_events(run_id="evaluation"))
        assert ledger["attempt_count"] == 2 and ledger["unknown_attempts"] == 1
        assert len(main.budget_status()["reservations"]) == 2
        assert sum(len(case._evaluation_bridge.events()) for case in cases) == 4
    finally:
        provider.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for case in cases:
            await case.close()
        await main.close()


@pytest.mark.parametrize("mode", ["offline", "live"])
async def test_offline_or_changed_provider_cannot_use_bridge(tmp_path, mode):
    main = await authority(tmp_path, ScriptedProvider([]), mode=mode)
    local = isolated(tmp_path, main)
    try:
        run = admitted(local)
        if mode == "live":
            local.settings = replace(local.settings, model="changed")
        with pytest.raises(ValueError, match="Offline|configuration changed"):
            await local._complete_with_retry(ModelRequest("", [], [], "scripted"), run, None)
        assert main.provider.requests == []
    finally:
        await local.close()
        await main.close()


async def test_retry_worker_and_unused_summary_are_scoped_once(tmp_path):
    provider = ScriptedProvider([
        {"error": "retry", "retryable": True},
        {"text": "worker", "usage": {"input_tokens": 10, "output_tokens": 2}},
        {"text": "summary", "usage": {"input_tokens": 4, "output_tokens": 1}},
    ])
    main = await authority(tmp_path, provider)
    local = isolated(tmp_path, main)
    try:
        parent = admitted(local, "parent")
        child_session = local.create_session()
        local.store.admit_run("child", child_session, [("run.admitted", {
            "project_id": local.default_workspace.project_id,
            "workspace_id": local.default_workspace.workspace_id,
            "parent_run_id": parent,
        }, "system", "runtime")])
        request = ModelRequest("", [], [], "scripted", 10)
        await local._complete_with_retry(request, "child", None)
        await local._complete_with_retry(request, parent, None, kind="context_summary")
        parent_usage = build_usage_ledger(local.model_usage_events(parent))
        child_usage = build_usage_ledger(local.model_usage_events("child"))
        assert parent_usage["attempt_count"] == 3 and parent_usage["unknown_attempts"] == 1
        assert parent_usage["reported_totals"] == {"input_tokens": 14, "output_tokens": 3}
        assert child_usage["attempt_count"] == 2
        assert [call["kind"] for call in parent_usage["calls"]] == [
            "worker", "worker", "context_summary",
        ]
        await local._emit(parent, "context.summary_generated", {
            "usage": {"input_tokens": 4, "output_tokens": 1},
        }, None)
        local_usage = build_usage_ledger(local.store.usage_events(run_id=parent))
        assert local_usage["attempt_count"] == 0 and not local_usage["complete"]
        assert local._evaluation_bridge.evidence(tmp_path)["usage"] == parent_usage
        assert len(main.budget_status()["reservations"]) == 3
        assert local.provider.requests == []
    finally:
        await local.close()
        await main.close()


def test_external_authority_marker_must_be_valid_not_silently_discard_usage():
    with pytest.raises(ValueError, match="authority"):
        build_usage_ledger([{
            "type": "model.response", "event_id": "response", "run_id": "local",
            "payload": {"usage": {"input_tokens": 10}, "usage_authority": {}},
        }])
