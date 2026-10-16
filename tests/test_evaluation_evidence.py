"""Evidence identity, unknown accounting, and partial report retention."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import eventide.evaluation as evaluation
from eventide.budget_prices import PriceVersion
from eventide.evaluation import run_evaluations
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


def _suite(root, cases):
    path = root / "evidence.json"
    path.write_text(json.dumps({"cases": cases}))
    return path


def _case(name="direct", **values):
    return {"id": name, "prompt": "answer", "script": [{"text": "done"}], **values}


def _read_report(report):
    saved = json.loads(Path(report["report_path"]).read_bytes())
    assert saved == json.loads(json.dumps(report))
    return saved


async def test_failure_kept_and_next_offline_case_runs(isolated_workspace, monkeypatch):
    original = evaluation._run_case

    async def fail_first(runtime, case, *args, **kwargs):
        if case["id"] == "broken":
            raise RuntimeError("synthetic grading failure")
        return await original(runtime, case, *args, **kwargs)

    monkeypatch.setattr(evaluation, "_run_case", fail_first)
    report = await run_evaluations(
        _suite(isolated_workspace, [_case("broken"), _case("good")]),
        settings=settings_for(isolated_workspace, api_key="in-memory-secret"),
    )
    assert report["status"] == "completed" and report["attempted"] == report["total"] == 2
    assert report["passed"] == 1
    failed = report["results"][0]
    assert failed["failure"]["type"] == "RuntimeError" and not failed["passed"]
    assert Path(failed["case_artifact_dir"], "case-result.json").is_file()
    assert Path(failed["evidence"]["database"]).is_file()
    _read_report(report)


async def test_configuration_and_canonical_run_fingerprints_are_traceable(isolated_workspace):
    report = await run_evaluations(
        _suite(isolated_workspace, [_case(generation={"max_output_tokens": 40})]),
        settings=settings_for(isolated_workspace),
    )
    item = report["results"][0]
    assert item["run_id"] and item["session_id"] and item["turn_id"]
    evidence = item["evidence"]
    assert evidence["main_run_id"] == item["run_id"]
    run = next(run for run in evidence["runs"] if run["run_id"] == item["run_id"])
    assert run["generation"]["max_output_tokens"] == 40
    assert run["context_fingerprints"]["tool_catalog_hash"]
    assert run["skill_snapshot"]["skills"] == []
    assert evidence["model_attempts"][0]["generation"]["max_output_tokens"] == 40
    assert evidence["usage"]["unknown_attempts"] == 1
    assert evidence["usage"]["reported_totals"] == {}
    assert item["usage"] == {}  # Missing usage is no longer a fabricated real zero.
    assert report["usage"]["unknown_attempts"] == 1
    assert evidence["costs"]["known_costs"] == {} and not evidence["costs"]["complete"]
    assert evidence["costs"]["unknown_attempts"] == 1
    for reference in (evidence["events"], report["provenance"]["inputs"]):
        digest = hashlib.sha256(Path(reference["path"]).read_bytes()).hexdigest()
        assert digest == reference["sha256"]
    assert report["provenance"]["runtime"]["commit"]
    assert report["provenance"]["runtime"]["code_sha256"]
    assert item["duration_ms"] >= item["agent_duration_ms"]


async def test_retry_unknown_and_fixed_prices_are_not_lost(isolated_workspace):
    prices = tuple(PriceVersion(
        f"test-{category}", "anthropic", "test-model", category, "CNY", rate,
        datetime(2020, 1, 1, tzinfo=timezone.utc),
    ) for category, rate in (("input_tokens", "1"), ("output_tokens", "2")))
    report = await run_evaluations(
        _suite(isolated_workspace, [_case(script=[
            {"error": "temporary", "retryable": True},
            {"text": "done", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ])]),
        settings=replace(
            settings_for(isolated_workspace), provider="anthropic", model="test-model",
        ),
        prices=prices,
    )
    item = report["results"][0]
    usage = item["evidence"]["usage"]
    assert report["passed"] == 1
    assert usage["attempt_count"] == 2 and usage["unknown_attempts"] == 1
    assert usage["reported_totals"] == {"input_tokens": 10, "output_tokens": 5}
    costs = item["evidence"]["costs"]
    assert costs["known_costs"] == {"CNY": "0.00002"}
    assert costs["unknown_attempts"] == 1 and not costs["complete"]
    versions = {price["version_id"] for price in item["evidence"]["price_versions"]}
    assert versions == {price.version_id for price in prices}
    used_versions = {line["price_version"] for snapshot in costs["latest"]
                     for currency in snapshot["estimates"].values() for line in currency["lines"]}
    assert used_versions == versions
    assert report["billing_applicable"] is False


async def test_timeout_keeps_unknown_attempt_and_runs_next_offline_case(isolated_workspace):
    report = await run_evaluations(
        _suite(isolated_workspace, [_case("slow", script=[{"delay_ms": 10_000, "text": "late"}]),
                                    _case("after")]),
        settings=settings_for(isolated_workspace), case_timeout=1,
    )
    first, second = report["results"]
    assert first["status"] == "timed_out" and first["failure"]["type"] == "TimeoutError"
    assert first["evidence"]["usage"]["unknown_attempts"] == 1
    assert first["evidence"]["costs"]["unknown_attempts"] == 1
    assert second["passed"]
    _read_report(report)


async def test_cancel_saves_partial_report_before_propagating(isolated_workspace, monkeypatch):
    dispatched = asyncio.Event()

    class ObservedProvider(ScriptedProvider):
        async def complete(self, request):
            dispatched.set()
            return await super().complete(request)

    monkeypatch.setattr(evaluation, "ScriptedProvider", ObservedProvider)
    output = isolated_workspace / "reports"
    task = asyncio.create_task(run_evaluations(
        _suite(isolated_workspace, [
            _case("slow", script=[{"delay_ms": 10_000}]), _case("waiting"),
        ]),
        settings=settings_for(isolated_workspace), output_dir=output,
    ))
    await asyncio.wait_for(dispatched.wait(), 10)
    assert json.loads((output / "offline-report.json").read_bytes())["status"] == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((output / "offline-report.json").read_bytes())
    assert report["status"] == "cancelled" and report["remaining"] == 1
    assert report["total"] == 2 and report["attempted"] == 1 and report["passed"] == 0
    entry = report["results"][0]
    assert entry["status"] == "cancelled"
    assert entry["evidence"]["usage"]["unknown_attempts"] == 1
    assert all(run["status"] == "interrupted" for run in entry["evidence"]["runs"])
    assert Path(entry["case_artifact_dir"], "case-result.json").is_file()


async def test_live_failure_stops_batch_and_keeps_unknown_usage(isolated_workspace, monkeypatch):
    calls = []

    def provider(settings):
        calls.append(settings)
        return ScriptedProvider([{"error": "Authentication failed for secret-value"}])

    monkeypatch.setattr(evaluation, "build_provider", provider)
    report = await run_evaluations(
        _suite(isolated_workspace, [_case("unauthorized"), _case("never-run")]), live=True,
        settings=settings_for(isolated_workspace, api_key="secret-value"),
    )
    assert len(calls) == 1
    assert report["status"] == "aborted" and report["attempted"] == 1 and report["remaining"] == 1
    assert report["total_input_tokens"] is None and report["total_output_tokens"] is None
    assert report["usage"]["unknown_attempts"] == 1
    assert "secret-value" not in json.dumps(report)
    for file in Path(report["artifact_dir"]).rglob("*"):
        if file.is_file() and file.suffix != ".pyc":
            assert b"secret-value" not in file.read_bytes(), file


async def test_observed_budget_stop_remains_failed_attempt(isolated_workspace):
    report = await run_evaluations(
        _suite(isolated_workspace, [_case(script=[
            {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "a"}}],
             "usage": {"input_tokens": 8, "output_tokens": 4}}, {"text": "should not happen"},
        ], workspace={"a": "source"})]),
        settings=replace(settings_for(isolated_workspace), run_token_limit=10),
    )
    item = report["results"][0]
    assert not item["passed"] and item["status"] == "interrupted"
    assert report["usage"]["reported_totals"] == {"input_tokens": 8, "output_tokens": 4}
    assert item["evidence"]["usage"]["attempt_count"] == 1


@pytest.mark.parametrize("timeout", [0, -1, 301, True, float("nan")])
async def test_invalid_timeout_rejected_before_credentials(timeout, monkeypatch):
    monkeypatch.setattr(evaluation.Settings, "from_env", lambda: pytest.fail("credentials read"))
    with pytest.raises(ValueError, match="case_timeout"):
        await run_evaluations(Path("unused"), case_timeout=timeout)


async def test_live_setup_exception_redacts_credentials_and_endpoint(
    isolated_workspace, monkeypatch,
):
    def broken(settings):
        raise RuntimeError(f"connection rejected: {settings.api_key} {settings.base_url}")

    monkeypatch.setattr(evaluation, "build_provider", broken)
    settings = replace(settings_for(isolated_workspace, api_key="memory-only-credential"),
                       base_url="https://secret-endpoint.invalid/private")
    report = await run_evaluations(
        _suite(isolated_workspace, [_case()]), live=True, settings=settings,
    )
    assert report["status"] == "aborted"
    assert report["results"][0]["failure"]["stage"] == "runtime"
    for file in Path(report["artifact_dir"]).rglob("*.json"):
        assert b"memory-only-credential" not in file.read_bytes()
        assert b"secret-endpoint.invalid" not in file.read_bytes()


async def test_default_limits_and_explicit_lower_limits_are_recorded(isolated_workspace):
    report = await run_evaluations(
        _suite(isolated_workspace, [_case()]),
        settings=replace(settings_for(isolated_workspace), max_steps=100, run_token_limit=0),
    )
    config = report["results"][0]["configuration"]
    assert config["max_steps"] == 12 and config["observed_token_limit"] == 20_000
    assert report["provenance"]["case_timeout"] == 300
    lower = await run_evaluations(
        _suite(isolated_workspace, [_case()]),
        settings=replace(settings_for(isolated_workspace), max_steps=3, run_token_limit=40),
        case_timeout=20,
    )
    config = lower["results"][0]["configuration"]
    assert config["max_steps"] == 3 and config["observed_token_limit"] == 40
    assert lower["provenance"]["case_timeout"] == 20


async def test_evidence_export_failure_cannot_be_a_pass(isolated_workspace, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("evidence disk failed")

    monkeypatch.setattr(evaluation, "case_evidence", broken)
    report = await run_evaluations(
        _suite(isolated_workspace, [_case()]), settings=settings_for(isolated_workspace),
    )
    item = report["results"][0]
    assert not item["passed"] and item["status"] == "failed"
    assert item["failure"]["stage"] == "evidence"
    assert "event evidence export failed" in item["reasons"]
    assert (Path(item["artifact_dir"]) / ".eventide" / "evals" / "offline.db").is_file()


async def test_holdout_split_is_mechanism_only_and_manifest_validated_first(
    isolated_workspace, monkeypatch,
):
    suite = Path(__file__).resolve().parents[1] / "evals" / "benchmark.yaml"
    captured = []
    real = evaluation._evaluate_case

    async def track(case, *args, **kwargs):
        captured.append(case["benchmark"]["split"])
        return await real(case, *args, **kwargs)

    monkeypatch.setattr(evaluation, "_evaluate_case", track)
    report = await run_evaluations(
        suite, settings=settings_for(isolated_workspace), split="holdout",
    )
    assert captured == ["holdout"] * 8
    assert report["total"] == report["passed"] == 8
    assert report["measurement_kind"] == "scripted_mechanism_validation"
    assert report["provenance"]["split"] == "holdout"
    with pytest.raises(ValueError, match="split requires"):
        await run_evaluations(suite, settings=settings_for(isolated_workspace), split="best-only")


async def test_missing_case_evidence_marks_total_incomplete(isolated_workspace, monkeypatch):
    original = evaluation.case_evidence

    def fail_first(store, directory, **kwargs):
        if directory.name == "case-001":
            raise OSError("one failed export")
        return original(store, directory, **kwargs)

    monkeypatch.setattr(evaluation, "case_evidence", fail_first)
    report = await run_evaluations(
        _suite(isolated_workspace, [_case(name, script=[
            {"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}},
        ]) for name in ("missing", "present")]),
        settings=settings_for(isolated_workspace),
    )
    assert report["passed"] == 1 and report["attempted"] == 2
    assert report["usage"]["covered_cases"] == 1
    assert report["usage"]["missing_evidence_cases"] == ["missing"]
    assert report["usage"]["complete"] is False
    assert report["usage"]["reported_totals"] == {"input_tokens": 3, "output_tokens": 1}


async def test_repeated_cancel_during_cleanup_still_saves_and_propagates(
    isolated_workspace, monkeypatch,
):
    closing, release = asyncio.Event(), asyncio.Event()
    original = evaluation.RuntimeHost.close

    async def delayed_close(self):
        closing.set()
        await release.wait()
        await original(self)

    monkeypatch.setattr(evaluation.RuntimeHost, "close", delayed_close)
    output = isolated_workspace / "reports"
    task = asyncio.create_task(run_evaluations(
        _suite(isolated_workspace, [_case("finished"), _case("waiting")]),
        settings=settings_for(isolated_workspace), output_dir=output,
    ))
    await asyncio.wait_for(closing.wait(), 10)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((output / "offline-report.json").read_bytes())
    assert report["status"] == "cancelled" and report["remaining"] == 1
    entry = report["results"][0]
    assert entry["status"] == "cancelled" and entry["run_id"]
    assert entry["evidence"]["runs"][0]["status"] == "completed"
    assert Path(entry["evidence"]["database"]).is_file()
    assert Path(entry["evidence"]["events"]["path"]).is_file()
    assert Path(entry["case_artifact_dir"], "case-result.json").is_file()


async def test_parent_worker_usage_is_exclusive_in_case_report(isolated_workspace):
    from eventide.evaluation import load_suite

    source = Path(__file__).resolve().parents[1] / "evals" / "workers.yaml"
    case = load_suite(source)[0]
    for response in case["script"]:
        response["usage"] = {"input_tokens": 10, "output_tokens": 2}
    report = await run_evaluations(
        _suite(isolated_workspace, [case]), settings=settings_for(isolated_workspace),
    )
    assert report["passed"] == 1
    evidence = report["results"][0]["evidence"]
    ledger = evidence["usage"]
    assert ledger["attempt_count"] == 4
    assert ledger["reported_totals"] == {"input_tokens": 40, "output_tokens": 8}
    assert [call["kind"] for call in ledger["calls"]].count("worker") == 2
    assert any(run.get("parent_run_id") for run in evidence["runs"])
    assert len({call["attempt_id"] for call in ledger["calls"]}) == 4


async def test_case_result_write_failure_keeps_batch_report(isolated_workspace, monkeypatch):
    original = evaluation.atomic_json

    def fail_result(path, value):
        if path.name == "case-result.json":
            raise OSError("case result export failed")
        return original(path, value)

    monkeypatch.setattr(evaluation, "atomic_json", fail_result)
    report = await run_evaluations(
        _suite(isolated_workspace, [_case()]), settings=settings_for(isolated_workspace),
    )
    assert report["passed"] == 0 and report["status"] == "completed"
    assert "case result export failed" in report["results"][0]["reasons"]
    assert Path(report["results"][0]["evidence"]["events"]["path"]).is_file()
    _read_report(report)


async def test_skill_version_and_loaded_fact_survive_evidence_export(isolated_workspace):
    case = _case(workspace={
        "skills/diagnose/SKILL.md":
            "---\nname: diagnose\ndescription: Diagnose a problem\n---\nRead the error first.\n",
    }, script=[
        {"tool_calls": [{"id": "load", "name": "load_skill",
                         "arguments": {"name": "diagnose"}}]}, {"text": "done"},
    ])
    report = await run_evaluations(
        _suite(isolated_workspace, [case]), settings=settings_for(isolated_workspace),
    )
    assert report["passed"] == 1
    item = report["results"][0]
    run = next(run for run in item["evidence"]["runs"] if run["run_id"] == item["run_id"])
    snapshot = run["skill_snapshot"]
    assert snapshot["catalog_hash"] == run["context_fingerprints"]["skill_catalog_hash"]
    assert snapshot["skills"][0]["version"] and snapshot["skills"][0]["manifest"]
    assert run["loaded_skills"][0]["version"] == snapshot["skills"][0]["version"]
