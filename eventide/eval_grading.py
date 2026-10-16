"""Independent benchmark graders around the existing Runtime and delivery APIs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from eventide.eval_suite import grade_facts
from eventide.host import RuntimeHost
from eventide.models import RunRequest, RunResult
from eventide.providers import ScriptedProvider
from eventide.workspace import git, workspace_checkpoint


def _fixture_hashes(case: dict[str, Any], root: Path) -> dict[str, str]:
    result = {}
    for relative in case["workspace"]:
        path = root / relative
        if (path.is_symlink() or not path.is_file() or path.resolve() != path
                or path.stat().st_size > 3 * 1024 * 1024):
            raise ValueError("Fixture file missing or no longer a regular file")
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _protected_reasons(
    case: dict[str, Any], root: Path, original: dict[str, str],
) -> tuple[str, ...]:
    try:
        observed = _fixture_hashes(case, root)
    except (ValueError, OSError):
        return ("protected fixture missing or linked",)
    allowed = set(case["grade"].get("allowed_changes", []))
    changed = {name for name, value in original.items() if observed[name] != value}
    reasons = []
    if changed - allowed:
        reasons.append("protected fixture changed: " + ", ".join(sorted(changed - allowed)))
    visible = {os.fsdecode(name) for name in git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z",
    ).split(b"\0") if name}
    if visible - original.keys():
        reasons.append("undeclared files created")
    if case["benchmark"]["category"] == "cross_file" and changed != allowed:
        reasons.append("cross-file repair must change every declared faulty source file")
    return tuple(reasons)


def _acceptance_command(workspace: Path, frozen: Path) -> str:
    import shlex

    code = (
        "import sys,unittest,json;"
        f"sys.path.insert(0,{str(workspace)!r});"
        f"suite=unittest.defaultTestLoader.discover({str(frozen)!r});"
        "result=unittest.TextTestRunner().run(suite);"
        "print('EVENTIDE_ACCEPTANCE='+json.dumps({'tests_run':result.testsRun}));"
        "sys.exit(0 if result.wasSuccessful() and result.testsRun else 1)"
    )
    return shlex.join([sys.executable, "-I", "-B", "-c", code])


def _tests_run(content: str) -> int | None:
    markers = [line.removeprefix("EVENTIDE_ACCEPTANCE=") for line in content.splitlines()
               if line.startswith("EVENTIDE_ACCEPTANCE=")]
    if len(markers) != 1:
        return None
    try:
        value = json.loads(markers[0])
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or set(value) != {"tests_run"}:
        return None
    count = value["tests_run"]
    return count if type(count) is int and count > 0 else None


def _accepted(result: dict[str, Any]) -> bool:
    return bool(result["passed"] and _tests_run(result["content"]) is not None)


def _frozen_manifest(frozen: Path) -> dict[str, str]:
    return {
        path.relative_to(frozen).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in frozen.rglob("*") if path.is_file() and not path.is_symlink()
    }


def _freeze_tests(source: Path) -> tuple[Path, dict[str, str]]:
    original = source / "tests"
    frozen = source.parent / "acceptance-tests"
    if frozen.exists():
        raise ValueError("Acceptance evidence directory already exists")
    shutil.copytree(original, frozen)
    manifest = _frozen_manifest(frozen)
    if not manifest:
        raise ValueError("No frozen acceptance tests")
    return frozen, manifest


async def _baseline(
    host: RuntimeHost, session: str, source: Path, frozen: Path,
) -> dict[str, Any]:
    host.create_session(session)
    result = await host.check_project(session, _acceptance_command(source, frozen))
    if result["passed"] or result["metadata"].get("exit_code") != 1:
        raise ValueError("Benchmark repair baseline must fail with exit code 1")
    if _tests_run(result["content"]) is None:
        raise ValueError("Benchmark baseline did not execute fixed acceptance tests")
    return result


async def run_benchmark_case(
    host: RuntimeHost, case: dict[str, Any], source: Path, session: str,
    *, fact_response_format: str | None = None,
) -> tuple[RunResult, dict[str, Any], tuple[str, ...]]:
    original = _fixture_hashes(case, source)
    checkpoint = workspace_checkpoint(source)
    if case["grade"]["kind"] == "facts":
        result = await host.run(RunRequest(
            case["prompt"], session, mode="plan", execution_target="snapshot",
            generation=({"response_format": fact_response_format} if fact_response_format else {}),
        ))
        reasons = grade_facts(case, result.output)
        source_unchanged = workspace_checkpoint(source) == checkpoint
        if not source_unchanged:
            reasons += ("read-only source changed",)
        return result, {
            "kind": "annotated_facts", "passed": not reasons,
            "source_unchanged": source_unchanged,
            "source_manifest": original, "execution": host.run_execution(result.run_id),
        }, reasons
    frozen, test_manifest = _freeze_tests(source)
    baseline = await _baseline(host, session, source, frozen)
    if case["grade"]["kind"] == "recovery_delivery":
        return await _delivery_case(host, case, source, frozen, original, baseline, test_manifest)
    result = await host.run(RunRequest(case["prompt"], session, execution_target="worktree"))
    execution = host.run_execution(result.run_id)
    candidate = Path(host.store.get_workspace(execution["execution_workspace_id"]).path)
    reasons = _protected_reasons(case, candidate, original)
    checked = await host.check_project(session, _acceptance_command(candidate, frozen))
    reasons += _protected_reasons(case, candidate, original)
    source_unchanged = workspace_checkpoint(source) == checkpoint
    if not source_unchanged:
        reasons += ("source changed before adoption",)
    if not _accepted(checked):
        reasons += ("independent acceptance failed",)
    if _frozen_manifest(frozen) != test_manifest:
        reasons += ("frozen acceptance evidence changed",)
    return result, {
        "kind": "fixed_tests", "baseline_run_id": baseline["run_id"],
        "baseline_exit_code": baseline["metadata"].get("exit_code"),
        "verification_run_id": checked["run_id"], "exit_code": checked["metadata"].get("exit_code"),
        "timed_out": checked["metadata"].get("timed_out", False),
        "baseline_tests_run": _tests_run(baseline["content"]),
        "tests_run": _tests_run(checked["content"]),
        "tests_unchanged": not _protected_reasons(case, candidate, original),
        "test_manifest": test_manifest, "frozen_tests": str(frozen),
        "source_unchanged": source_unchanged, "execution": execution,
        "candidate": str(candidate), "passed": not reasons,
    }, tuple(dict.fromkeys(reasons))


async def _delivery_case(
    host: RuntimeHost, case: dict[str, Any], source: Path, frozen: Path,
    original: dict[str, str], baseline: dict[str, Any], test_manifest: dict[str, str],
) -> tuple[RunResult, dict[str, Any], tuple[str, ...]]:
    prepared = await host.manage_delivery(
        "prepare", name=case["id"], protected_patterns=("tests/**", "README.md"),
    )
    delivery_id = prepared["delivery_id"]
    # The evaluation preset is case-local user authorization. Delivery Writers
    # have another Workspace, so explicitly pin the same versions there before
    # the normal Run admission freezes them. Never put Skills in the task fixture.
    skill_config = host.skills.configuration(host.default_workspace.workspace_id)
    if skill_config["enabled_user"]:
        checkout = host.resolve_or_register_workspace(Path(prepared["receipt"]["checkout"]))
        host.configure_skills(
            checkout.workspace_id, enabled_user=skill_config["enabled_user"],
            disabled_project=[], selected=skill_config["selected"],
        )
    writer = await host.manage_delivery("run", delivery_id=delivery_id, goal=case["prompt"])
    row = host.store.get_run(writer["worker_run_id"])
    identity = host.store.get_run_identity(writer["worker_run_id"])
    assert row is not None and identity is not None
    result = RunResult(
        writer["worker_run_id"], writer["worker_session_id"], writer["status"], writer["output"],
        row["steps"], row["tool_calls"], row["duration_ms"], writer["usage"], identity["turn_id"],
    )
    repair_by = (
        "main_runtime_provider_tools" if host._evaluation_bridge is not None
        else "scripted_provider_tools" if isinstance(host.provider, ScriptedProvider)
        else "configured_provider_tools"
    )
    settings, store = host.settings, host.store
    await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([]), store=store, enable_mcp=False)
    try:
        recovered = await reopened.manage_delivery("show", delivery_id=delivery_id)
        candidate = Path(prepared["receipt"]["checkout"])
        reasons = _protected_reasons(case, candidate, original)
        checked = await reopened.manage_delivery(
            "verify", delivery_id=delivery_id,
            checks=[_acceptance_command(candidate, frozen)],
        )
        reasons += _protected_reasons(case, candidate, original)
        source_unchanged = _fixture_hashes(case, source) == original
        check_counts = [_tests_run(item["content"]) for item in checked.get("checks", [])]
        if (checked["status"] != "passed" or not source_unchanged
                or len(check_counts) != 1 or check_counts[0] is None):
            reasons += ("independent delivery verification failed",)
        evidence: dict[str, Any] = {
            "kind": "delivery_lifecycle", "scenario": case["grade"]["scenario"],
            "delivery_id": delivery_id, "recovered_after_restart": recovered["delivery_id"]
            == delivery_id, "baseline_run_id": baseline["run_id"],
            "baseline_exit_code": baseline["metadata"].get("exit_code"),
            "baseline_tests_run": _tests_run(baseline["content"]),
            "verification_run_id": checked["control_run_id"],
            "verification_tests_run": check_counts[0] if check_counts else None,
            "test_manifest": test_manifest, "frozen_tests": str(frozen),
            "candidate": str(candidate), "source_unchanged_before_adoption": source_unchanged,
            "patch_sha256": checked.get("patch_sha256"),
            "control_actions_by": "evaluation_harness", "repair_by": repair_by,
        }
        if not reasons and case["grade"]["scenario"] == "restart_then_adopt":
            adopted = await reopened.manage_delivery("apply", delivery_id=delivery_id)
            evidence["adoption_run_id"] = adopted["control_run_id"]
            evidence["patch_hash_matches"] = adopted["patch_sha256"] == checked["patch_sha256"]
            source_session = reopened.create_session()
            final = await reopened.check_project(
                source_session, _acceptance_command(source, frozen),
            )
            evidence["source_verification_run_id"] = final["run_id"]
            evidence["exit_code"] = final["metadata"].get("exit_code")
            evidence["tests_run"] = _tests_run(final["content"])
            if not _accepted(final) or not evidence["patch_hash_matches"]:
                reasons += ("adopted source failed independent acceptance",)
        elif not reasons:
            injected = source / "external-review.txt"
            injected.write_text("external source change after verification\n", encoding="utf-8")
            evidence["injected_fault"] = {
                "actor": "evaluation_harness", "path": injected.name,
                "purpose": "source_drift_after_verification",
            }
            try:
                await reopened.manage_delivery("apply", delivery_id=delivery_id)
            except ValueError as exc:
                evidence["adoption_refused"] = "Source" in str(exc) and "changed" in str(exc)
            else:
                evidence["adoption_refused"] = False
            evidence["source_business_unchanged"] = _fixture_hashes(case, source) == original
            if not evidence["adoption_refused"] or not evidence["source_business_unchanged"]:
                reasons += ("stale verification was adopted or source changed",)
        if _frozen_manifest(frozen) != test_manifest:
            reasons += ("frozen acceptance evidence changed",)
        evidence["candidate_retained"] = candidate.is_dir()
        evidence["tests_unchanged"] = not _protected_reasons(case, candidate, original)
        evidence["passed"] = not reasons
        return result, evidence, reasons
    finally:
        await reopened.close()
