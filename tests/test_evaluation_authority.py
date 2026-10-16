"""Controlled evaluation execution cannot create a second provider or bill."""

import json
from pathlib import Path

import pytest

from eventide.evaluation import run_evaluations
from eventide.evaluation_quote import evaluation_quote
from eventide.providers import ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_evaluation_calls import authority


async def test_controlled_case_report_uses_main_calls_and_frozen_generation(tmp_path, monkeypatch):
    cases = [
        {"id": "one", "prompt": "answer", "workspace": {}, "expected": {"final_contains": "done"}}
    ]
    main = await authority(
        tmp_path,
        ScriptedProvider(
            [
                {"text": "done", "usage": {"input_tokens": 10, "output_tokens": 2}},
            ]
        ),
        cases=cases,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("controlled case accessed another provider/configuration")

    monkeypatch.setattr("eventide.evaluation.build_provider", forbidden)
    monkeypatch.setattr("eventide.evaluation.resolve_provider_configuration", forbidden)
    try:
        report = await run_evaluations(
            Path("builtin:controlled"),
            live=True,
            settings=main.settings,
            cases_snapshot=cases,
            suite_metadata={"manifest_sha256": "frozen"},
            call_authority=(main, "evaluation"),
        )
        assert report["passed"] == 1
        assert report["measurement_kind"] == "provider_experiment"
        assert report["usage"]["attempt_count"] == 1
        case = report["results"][0]
        evidence = case["evidence"]
        assert evidence["source"] == "eventide.execution_and_model_authorities"
        assert evidence["usage"]["reported_totals"] == {"input_tokens": 10, "output_tokens": 2}
        assert evidence["model_authority"]["database"] == str(main.store.path)
        local_events = json.loads(Path(evidence["events"]["path"]).read_bytes())
        assert not any(event["type"].startswith("model.attempt_") for event in local_events)
        assert build_usage_ledger(local_events)["attempt_count"] == 0
        assert not build_usage_ledger(local_events)["complete"]
        assert len(main.provider.requests) == 1
    finally:
        await main.close()


async def test_controlled_case_rejects_changed_inputs_before_any_call(tmp_path):
    cases = [{"id": "one", "prompt": "answer", "workspace": {}}]
    main = await authority(tmp_path, ScriptedProvider([]), cases=cases)
    try:
        with pytest.raises(ValueError, match="frozen admission"):
            await run_evaluations(
                Path("builtin:controlled"),
                live=True,
                settings=main.settings,
                cases_snapshot=[{**cases[0], "prompt": "changed"}],
                suite_metadata={"manifest_sha256": "frozen"},
                call_authority=(main, "evaluation"),
            )
        assert main.provider.requests == []
    finally:
        await main.close()


async def test_controller_frozen_development_cases_share_service_ledger(tmp_path):
    from eventide.bundled_evaluations import load_builtin_suite
    from eventide.evaluation_control import EvaluationController
    from tests.test_evaluation_control import host_for

    suite = load_builtin_suite("engineering-v1", split="development")
    script = [entry for case in suite["cases"] for entry in case["script"]]
    host = host_for(tmp_path)
    host.provider = ScriptedProvider(script)
    controller = EvaluationController(host)
    before = host._workspace_checkpoint(Path(host.default_workspace.path))
    try:
        quote = evaluation_quote(host, host.default_workspace.project_id)
        await controller.admit(
            host.default_workspace.project_id,
            "controlled",
            suite_id=suite["id"],
            suite_sha256=suite["suite_sha256"],
            mode="live",
            confirmed=True,
            quote_hash=quote["quote_hash"],
        )
        # New defaults apply to future Runs; the accepted experiment keeps its snapshot.
        host.configure_generation(host.settings.model, {"max_output_tokens": 15})
        result = await controller.start("controlled")
        report = controller.report("controlled")
        assert result.status == "completed"
        assert report["passed"] == report["total"] == 12
        ledger = build_usage_ledger(host.store.usage_events(run_id="controlled"))
        assert ledger["attempt_count"] == report["usage"]["attempt_count"]
        assert ledger["attempt_count"] == len(host.provider.requests) > 12
        assert all(request.max_tokens != 15 for request in host.provider.requests)
        assert report["usage"]["source"] == "exclusive_model_authorities"
        assert host._workspace_checkpoint(Path(host.default_workspace.path)) == before
        assert controller.detail("controlled")["mode"] == "live"
        assert controller.detail("controlled")["billing_applicable"] is True
    finally:
        await host.close()


async def test_controller_requires_confirmed_current_model_and_stops_on_global_budget(tmp_path):
    from eventide.budgets import BudgetScope
    from eventide.bundled_evaluations import builtin_suite_catalog
    from eventide.evaluation_control import EvaluationController
    from tests.test_evaluation_control import host_for

    host = host_for(tmp_path)
    controller = EvaluationController(host)
    body = {
        "suite_id": "engineering-v1",
        "suite_sha256": builtin_suite_catalog()[0]["suite_sha256"],
        "mode": "live",
    }
    project = host.default_workspace.project_id
    try:
        with pytest.raises(ValueError, match="confirmation"):
            await controller.admit(project, "unconfirmed", **body)
        with pytest.raises(ValueError, match="configuration changed"):
            await controller.admit(
                project, "stale", confirmed=True, quote_hash="stale", **body
            )
        host.configure_budget(BudgetScope("project", project), token_limit=0, request_id="stop")
        await controller.admit(
            project,
            "blocked",
            confirmed=True,
            quote_hash=evaluation_quote(host, host.default_workspace.project_id)["quote_hash"],
            **body,
        )
        result = await controller.start("blocked")
        assert result.status == "interrupted"
        report = controller.report("blocked")
        assert report["status"] == "aborted" and report["attempted"] == 1
        assert host.provider.requests == []
        assert report["usage"]["attempt_count"] == 0
    finally:
        await host.close()


async def test_live_admission_restart_parks_without_reconstructing_requests(tmp_path):
    from eventide.bundled_evaluations import builtin_suite_catalog
    from eventide.evaluation_control import EvaluationController
    from eventide.host import RuntimeHost
    from tests.test_evaluation_control import host_for

    host = host_for(tmp_path)
    settings = host.settings
    controller = EvaluationController(host)
    await controller.admit(
        host.default_workspace.project_id, "pending-live", suite_id="engineering-v1",
        suite_sha256=builtin_suite_catalog()[0]["suite_sha256"],
        mode="live", confirmed=True,
        quote_hash=evaluation_quote(host, host.default_workspace.project_id)["quote_hash"],
    )
    host._closed = True
    host.store.close()
    host._lease.close()
    reopened = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        current = EvaluationController(reopened)
        assert current.detail("pending-live")["status"] == "interrupted"
        assert current.detail("pending-live")["mode"] == "live"
        with pytest.raises(ValueError, match="awaiting"):
            current.start("pending-live")
        assert reopened.provider.requests == []
        assert reopened.budget_status()["reservations"] == []
    finally:
        await reopened.close()
