"""Frozen dataset boundaries and independent scripted mechanism validation."""

import copy
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
import yaml

import eventide.evaluation as evaluation
from eventide.eval_suite import BenchmarkTask, grade_facts, load_benchmark, relative_file
from eventide.evaluation import load_suite, run_evaluations
from tests.test_runtime import settings_for

SUITE = Path(__file__).resolve().parents[1] / "evals" / "benchmark.yaml"
PACKAGED_SUITE = SUITE.parents[1] / "eventide/bundled_evals/engineering-v1/manifest.yaml"


def test_frozen_benchmark_categories_and_development_provenance():
    cases = load_suite(SUITE)
    assert len(cases) == 20
    assert Counter(case["benchmark"]["split"] for case in cases) == {
        "development": 12, "holdout": 8,
    }
    assert Counter(case["benchmark"]["category"] for case in cases) == {
        "understanding": 4, "diagnosis": 4, "single_file": 6, "cross_file": 4,
        "recovery_delivery": 2,
    }
    assert len({case["benchmark"]["suite_sha256"] for case in cases}) == 1
    old = load_suite(SUITE.with_name("engineering.yaml"))
    for original in old:
        match = next(case for case in cases if case["workspace"] == original["workspace"])
        assert match["benchmark"]["split"] == "development"
        assert match["script"] == original["script"]


def _copy_suite(tmp_path):
    root = tmp_path / "evals"
    shutil.copytree(PACKAGED_SUITE.parent / "fixtures", root / "fixtures")
    destination = root / "benchmark.yaml"
    destination.write_bytes(PACKAGED_SUITE.read_bytes())
    return destination, yaml.safe_load(destination.read_text())


def _change_task(path, manifest, index, change):
    reference = manifest["tasks"][index]
    task_path = path.parent / reference["path"]
    task = json.loads(task_path.read_bytes())
    change(task)
    body = json.dumps(task).encode()
    task_path.write_bytes(body)
    reference["sha256"] = hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("mutation,match", [
    ("digest", "Frozen task changed"), ("duplicate", "unique"),
    ("split", "12 development"), ("family", "family cannot cross"),
    ("symlink", "linked"), ("extra", "Extra inputs"),
])
def test_manifest_rejects_changed_or_ambiguous_inputs(tmp_path, mutation, match):
    path, manifest = _copy_suite(tmp_path)
    if mutation == "digest":
        (path.parent / manifest["tasks"][0]["path"]).write_text("{}")
    elif mutation == "duplicate":
        manifest["tasks"][1] = copy.deepcopy(manifest["tasks"][0])
    elif mutation == "split":
        _change_task(path, manifest, 0, lambda task: task.update(split="holdout"))
    elif mutation == "family":
        first = json.loads((path.parent / manifest["tasks"][0]["path"]).read_bytes())
        _change_task(path, manifest, 2, lambda task: task.update(family=first["family"]))
    elif mutation == "symlink":
        original = path.parent / manifest["tasks"][0]["path"]
        body = original.read_bytes()
        original.unlink()
        other = tmp_path / "outside.json"
        other.write_bytes(body)
        original.symlink_to(other)
    else:
        manifest["unreviewed_option"] = True
    with pytest.raises(ValueError, match=match):
        load_benchmark(path, manifest)


@pytest.mark.parametrize("path", [
    "../outside", "/absolute", "a/../b", "a\\b", "./a", ".", ".git/config", ".eventide/config",
])
def test_fixture_paths_are_canonical_and_do_not_replace_state(path):
    with pytest.raises(ValueError, match="canonical"):
        relative_file(path)


def test_task_rejects_incorrect_citations_and_protected_allowed_changes():
    manifest = yaml.safe_load(PACKAGED_SUITE.read_text())
    first = json.loads((PACKAGED_SUITE.parent / manifest["tasks"][0]["path"]).read_bytes())
    first["grade"]["facts"][0]["end_line"] = 1000
    with pytest.raises(ValueError, match="existing fixture lines"):
        BenchmarkTask.model_validate(first)
    repair = json.loads((PACKAGED_SUITE.parent / manifest["tasks"][8]["path"]).read_bytes())
    repair["grade"]["allowed_changes"] = ["README.md"]
    with pytest.raises(ValueError, match="business source"):
        BenchmarkTask.model_validate(repair)


@pytest.mark.parametrize("change", ["answer", "citation", "duplicate", "extra", "not-json"])
def test_fact_grader_requires_claims_and_source_references(change):
    case = load_suite(SUITE)[0]
    answer = {"facts": copy.deepcopy(case["grade"]["facts"])}
    assert grade_facts(case, json.dumps(answer)) == ()
    if change == "answer":
        answer["facts"][0]["answer"] = "invented"
    elif change == "citation":
        answer["facts"][0]["start_line"] += 1
    elif change == "duplicate":
        answer["facts"][1] = answer["facts"][0]
    elif change == "extra":
        answer["score"] = "passed"
    assert grade_facts(case, "done" if change == "not-json" else json.dumps(answer))


async def test_live_benchmark_rejected_before_resolving_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("benchmark tried to read provider settings")
    monkeypatch.setattr(evaluation, "resolve_provider_configuration", forbidden)
    monkeypatch.setattr(evaluation.Settings, "from_env", forbidden)
    with pytest.raises(ValueError, match="live experiments are not enabled"):
        await run_evaluations(SUITE, live=True)


async def test_all_twenty_fixtures_use_real_runtime_and_independent_grading(
    isolated_workspace, monkeypatch,
):
    def forbidden(*args, **kwargs):
        pytest.fail("offline benchmark attempted provider credential lookup")
    monkeypatch.setattr(evaluation, "resolve_provider_configuration", forbidden)
    monkeypatch.setattr(evaluation, "build_provider", forbidden)
    report = await run_evaluations(SUITE, settings=settings_for(isolated_workspace))
    assert report["measurement_kind"] == "scripted_mechanism_validation"
    assert report["passed"] == report["total"] == 20, report["failure_reasons"]
    assert report["regression_smoke"] is True
    assert Counter(item["benchmark"]["split"] for item in report["results"]) == {
        "development": 12, "holdout": 8,
    }
    for item in report["results"]:
        source = Path(item["artifact_dir"])
        assert (source.parent / "state" / "evals" / "offline.db").is_file()
        assert not (source / ".eventide").exists()
        evidence = item["verification"]
        assert evidence["passed"]
        if evidence["kind"] == "annotated_facts":
            assert evidence["source_unchanged"] and evidence["execution"]["kind"] == "snapshot"
        else:
            assert evidence["baseline_exit_code"] == 1
            assert evidence["verification_run_id"] != evidence["baseline_run_id"]
            for filename, digest in evidence["test_manifest"].items():
                body = (Path(evidence["frozen_tests"]) / filename).read_bytes()
                assert hashlib.sha256(body).hexdigest() == digest
            if evidence["kind"] == "fixed_tests":
                assert evidence["exit_code"] == 0 and evidence["source_unchanged"]
                assert evidence["execution"]["kind"] == "worktree"
                assert Path(evidence["candidate"]) != source
            else:
                assert evidence["recovered_after_restart"] and evidence["candidate_retained"]
                assert evidence["control_actions_by"] == "evaluation_harness"
                assert evidence["repair_by"] == "scripted_provider_tools"
                if evidence["scenario"] == "restart_then_adopt":
                    assert evidence["patch_hash_matches"] and evidence["exit_code"] == 0
                else:
                    assert evidence["adoption_refused"] and evidence["source_business_unchanged"]
    assert not (isolated_workspace / ".eventide").exists()


@pytest.mark.parametrize("attack,expected", [
    ("claim_only", "independent acceptance failed"),
    ("protected_test", "protected fixture changed"),
    ("extra_file", "undeclared files created"),
    ("partial_cross", "cross-file repair must change every"),
])
async def test_repair_output_cannot_replace_frozen_acceptance(
    isolated_workspace, monkeypatch, attack, expected,
):
    cases = load_suite(SUITE)
    case = copy.deepcopy(next(item for item in cases if item["id"] == (
        "invoice-total-and-format" if attack == "partial_cross" else "slug-whitespace"
    )))
    if attack == "claim_only":
        case["script"] = [{"text": "All tests passed, trust me."}]
    elif attack == "partial_cross":
        case["script"][0]["tool_calls"] = case["script"][0]["tool_calls"][:1]
    else:
        path = "tests/test_contract.py" if attack == "protected_test" else "surprise.py"
        if attack == "protected_test":
            path = next(name for name in case["workspace"] if name.startswith("tests/test_"))
        case["script"][0]["tool_calls"].append({
            "id": "unreviewed-write", "name": "write_file",
            "arguments": {"path": path, "content": "# pretend the tests passed\n"},
        })
    monkeypatch.setattr(evaluation, "load_suite", lambda _: [case])
    report = await run_evaluations(SUITE, settings=settings_for(isolated_workspace))
    assert report["passed"] == 0
    assert any(expected in reason for reason in report["results"][0]["reasons"])


@pytest.mark.parametrize("content", [
    "EVENTIDE_ACCEPTANCE=", 'EVENTIDE_ACCEPTANCE={"tests_run":0}',
    'EVENTIDE_ACCEPTANCE={"tests_run":true}', 'EVENTIDE_ACCEPTANCE={"tests_run":-1}',
    'EVENTIDE_ACCEPTANCE={"tests_run":1,"passed":true}',
    'EVENTIDE_ACCEPTANCE={"tests_run":1}\nEVENTIDE_ACCEPTANCE={"tests_run":1}',
    'fake EVENTIDE_ACCEPTANCE={"tests_run":1}',
])
def test_acceptance_requires_one_valid_positive_test_count(content):
    from eventide.eval_grading import _tests_run

    assert _tests_run(content) is None
    assert _tests_run('log\nEVENTIDE_ACCEPTANCE={"tests_run":7}\n') == 7
