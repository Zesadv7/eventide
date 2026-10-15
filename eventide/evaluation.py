"""Deterministic offline and opt-in live evaluation runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from eventide.budget_prices import PriceVersion
from eventide.config import Settings
from eventide.eval_evidence import (
    atomic_json,
    case_evidence,
    runtime_revision,
    safe_configuration,
    safe_failure,
    summarize_usage,
)
from eventide.eval_grading import _tests_run, run_benchmark_case
from eventide.eval_suite import load_benchmark
from eventide.generation import GenerationPatch
from eventide.host import RuntimeHost
from eventide.models import RunRequest, RunResult
from eventide.normalization import normalize
from eventide.provider_config import resolve_provider_configuration
from eventide.providers import ScriptedProvider, build_provider
from eventide.rag.index import IndexStore
from eventide.store import RuntimeStore


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    id: str
    passed: bool
    output: str
    duration_ms: float
    tool_calls: int
    tool_errors: int
    safety_blocks: int
    reasons: tuple[str, ...]
    steps: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    status: str = "unknown"
    verification: dict[str, Any] = field(default_factory=dict)
    artifact_dir: str | None = None
    benchmark: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, str] | None = None
    run_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    case_artifact_dir: str | None = None
    agent_duration_ms: float | None = None


def load_suite(path: Path) -> list[dict[str, Any]]:
    import yaml

    from eventide.bundled_evaluations import load_builtin_alias

    if path.absolute().resolve() != path.absolute():
        raise ValueError("Evaluation suite must not be linked")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "builtin_suite" in data:
        return load_builtin_alias(data)["cases"]
    if isinstance(data, dict) and "schema_version" in data:
        return load_benchmark(path, data)
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("Evaluation suite must contain a 'cases' list")
    if any(not isinstance(case, dict) or "prompt" not in case for case in cases):
        raise ValueError("Evaluation cases must be objects with a prompt")
    return cases


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def _create_sandbox(fixtures: dict[str, Any], *, parent: Path | None = None) -> Path:
    """Materialize one case's fixtures in a throwaway Git workspace.

    The sandbox is deliberately not auto-deleted: failed cases keep their
    workspace, database, and git history around for debugging. The eval state
    root is private to the case; the repo-local git identity means later
    commits (seeded or model-driven) need no global git configuration.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="eventide-eval-", dir=parent)).resolve()
    for relative, content in fixtures.items():
        path = Path(str(relative))
        if path.is_absolute() or ".." in path.parts or "\\" in str(relative):
            raise ValueError("Evaluation fixture must stay inside its sandbox")
        target = sandbox / str(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    _run_git(sandbox, "init")
    _run_git(sandbox, "config", "user.name", "eventide-eval")
    _run_git(sandbox, "config", "user.email", "eval@localhost")
    _run_git(sandbox, "config", "commit.gpgsign", "false")
    # Keep the eval's own state root out of git evidence such as porcelain status.
    with (sandbox / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(".eventide/\n")
    _run_git(sandbox, "add", "-A")
    _run_git(sandbox, "commit", "--allow-empty", "-m", "eval fixture seed")
    return sandbox


def _test_manifest(root: Path) -> dict[str, str]:
    tests = root / "tests"
    if tests.is_symlink() or not tests.is_dir():
        raise ValueError("acceptance_tests requires a real tests directory")
    manifest: dict[str, str] = {}
    for path in sorted(tests.rglob("*")):
        if path.is_symlink():
            raise ValueError("Acceptance tests cannot contain symbolic links")
        if "__pycache__" in path.parts or path.suffix == ".pyc" or not path.is_file():
            continue
        body = path.read_bytes()
        if len(body) > 1024 * 1024 or len(manifest) >= 2000:
            raise ValueError("Acceptance tests exceed their bound")
        manifest[path.relative_to(tests).as_posix()] = hashlib.sha256(body).hexdigest()
    if not manifest:
        raise ValueError("Acceptance tests cannot be empty")
    return manifest


def _acceptance_command(workspace: Path, frozen_tests: Path) -> str:
    # Import the stdlib runner before adding candidate code to the import path.
    code = (
        "import sys,unittest,json;"
        f"sys.path.insert(0,{str(workspace)!r});"
        f"suite=unittest.defaultTestLoader.discover({str(frozen_tests)!r});"
        "result=unittest.TextTestRunner().run(suite);"
        "print('EVENTIDE_ACCEPTANCE='+json.dumps({'tests_run':result.testsRun}));"
        "sys.exit(0 if result.wasSuccessful() and result.testsRun else 1)"
    )
    return shlex.join([sys.executable, "-I", "-B", "-c", code])


def _clear_candidate_bytecode(workspace: Path) -> None:
    # These are disposable eval fixtures, never a user's project.
    for path in workspace.rglob("__pycache__"):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


async def _run_case(
    runtime: RuntimeHost,
    case: dict[str, Any],
    sandbox: Path | None,
    session_id: str,
) -> tuple[RunResult, dict[str, Any], tuple[str, ...]]:
    if "benchmark" in case:
        assert sandbox is not None
        return await run_benchmark_case(runtime, case, sandbox, session_id)
    request = RunRequest(
        prompt=str(case["prompt"]), session_id=session_id,
        generation=GenerationPatch.model_validate(case.get("generation", {})).patch(),
    )
    if case.get("acceptance_tests") is not True:
        return await runtime.run(request), {}, ()
    if sandbox is None:
        raise ValueError("acceptance_tests requires an inline workspace fixture")
    original = _test_manifest(sandbox)
    runtime.create_session(session_id)
    frozen = sandbox.parent / "acceptance-evidence" / "tests"
    shutil.copytree(
        sandbox / "tests", frozen, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    command = _acceptance_command(sandbox, frozen)
    baseline = await runtime.check_project(session_id, command)
    if baseline["passed"] or baseline["metadata"].get("exit_code") != 1:
        raise ValueError("Acceptance fixture must have a failing baseline (exit code 1)")
    if _tests_run(baseline["content"]) is None:
        raise ValueError("Acceptance baseline did not run the fixed test runner")
    result = await runtime.run(request)
    try:
        unchanged = _test_manifest(sandbox) == original
    except (OSError, ValueError):
        unchanged = False
    evidence = {
        "baseline_run_id": baseline["run_id"],
        "baseline_exit_code": baseline["metadata"]["exit_code"],
        "tests_unchanged": unchanged,
        "test_manifest": original,
        "frozen_tests": str(frozen),
        "baseline_tests_run": _tests_run(baseline["content"]),
    }
    if not unchanged:
        return result, {**evidence, "passed": False}, ("acceptance tests changed",)
    _clear_candidate_bytecode(sandbox)
    checked = await runtime.check_project(session_id, command)
    unchanged = _test_manifest(sandbox) == original
    frozen_unchanged = _test_manifest(frozen.parent) == original
    accepted = (checked["passed"] and unchanged and frozen_unchanged
                and _tests_run(checked["content"]) is not None)
    evidence.update(
        {
            "verification_run_id": checked["run_id"],
            "exit_code": checked["metadata"].get("exit_code"),
            "timed_out": checked["metadata"].get("timed_out", False),
            "tests_unchanged": unchanged,
            "frozen_tests_unchanged": frozen_unchanged,
            "tests_run": _tests_run(checked["content"]),
            "passed": accepted,
        }
    )
    return result, evidence, () if accepted else ("independent acceptance failed",)


def _git_reasons(workdir: Path) -> list[str]:
    if _run_git(workdir, "rev-parse", "--verify", "HEAD").returncode:
        return ["git evidence unavailable"]
    try:
        count = int(_run_git(workdir, "rev-list", "--count", "HEAD").stdout.strip() or 0)
    except ValueError:
        return ["git evidence unavailable"]
    if count <= 1:
        return ["work was not committed"]
    if _run_git(workdir, "status", "--porcelain").stdout.strip():
        return ["uncommitted changes remain in the sandbox"]
    return []


def _filesystem_reasons(expected: dict[str, Any], workdir: Path | None) -> list[str]:
    """Deterministic fs/git grading; model output is never completion evidence."""
    keys = ("file_exists", "file_absent", "file_contains")
    if not any(expected.get(key) for key in keys) and expected.get("git_committed") is not True:
        return []
    if workdir is None:
        return ["no sandbox workspace for file checks"]
    reasons: list[str] = []
    for relative in expected.get("file_exists", []):
        if not (workdir / str(relative)).is_file():
            reasons.append(f"missing file: {relative}")
    for relative in expected.get("file_absent", []):
        if (workdir / str(relative)).exists():
            reasons.append(f"unexpected file: {relative}")
    contains = expected.get("file_contains")
    if isinstance(contains, dict):
        for relative, needle in contains.items():
            try:
                text = (workdir / str(relative)).read_text(encoding="utf-8")
            except OSError:
                reasons.append(f"unreadable file: {relative}")
                continue
            if str(needle).lower() not in text.lower():
                reasons.append(f"{relative} missing {needle!r}")
    if expected.get("git_committed") is True:
        reasons.extend(_git_reasons(workdir))
    return reasons


def _judge(
    case: dict[str, Any],
    output: str,
    events: list[dict[str, Any]],
    status: str,
    workdir: Path | None = None,
) -> tuple[bool, tuple[str, ...]]:
    expected = case.get("expected", {})
    reasons: list[str] = []
    if status != expected.get("status", "completed"):
        reasons.append(f"status={status}")
    needle = expected.get("final_contains")
    if needle and str(needle).lower() not in output.lower():
        reasons.append(f"final output missing {needle!r}")
    used = [event["payload"].get("name") for event in events if event["type"] == "tool.request"]
    for name in expected.get("required_tools", []):
        if name not in used:
            reasons.append(f"required tool not called: {name}")
    successful = {
        event["payload"].get("name")
        for event in events
        if event["type"] == "tool.result" and event["payload"].get("is_error") is False
    }
    for name in expected.get("successful_tools", []):
        if name not in successful:
            reasons.append(f"required tool did not succeed: {name}")
    denied = any(
        event["type"] == "tool.result"
        and "Permission denied" in str(event["payload"].get("content", ""))
        for event in events
    )
    if expected.get("permission_denied") is True and not denied:
        reasons.append("expected a permission denial")
    reasons.extend(_filesystem_reasons(expected, workdir))
    return not reasons, tuple(reasons)


async def _bounded_case(
    runtime: RuntimeHost, case: dict[str, Any], sandbox: Path, session_id: str,
    timeout: float,
) -> tuple[RunResult, dict[str, Any], tuple[str, ...]]:
    task = asyncio.create_task(_run_case(runtime, case, sandbox, session_id))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise TimeoutError("Evaluation case exceeded its wall-clock limit")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            # Host cancellation may return an interrupted result after side-effect cleanup.
            while not task.done():
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(task)
        if not task.cancelled():
            task.exception()  # Observe failures even when a timeout/cancel owns the result.


async def _evaluate_case(
    case: dict[str, Any], index: int, settings: Settings, directory: Path, *,
    live: bool, prices: tuple[PriceVersion, ...], case_timeout: float,
) -> tuple[EvalCaseResult, bool]:
    mode = "live" if live else "offline"
    started = time.perf_counter()
    sandbox = None
    store = None
    runtime = None
    result = None
    verification: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    reasons: tuple[str, ...] = ()
    failure = None
    cancelled = False
    stage = "fixture"
    case_settings = settings
    try:
        directory.mkdir()
        atomic_json(directory / "case-input.json", normalize(case, _secrets(settings)))
        fixtures = case.get("workspace", {})
        if not isinstance(fixtures, dict):
            raise ValueError("Evaluation workspace must be an inline fixture mapping")
        sandbox = _create_sandbox(fixtures, parent=directory)
        case_settings = replace(
            settings, workdir=sandbox,
            state_dir=directory / "state" if "benchmark" in case else sandbox / ".eventide",
            workers_enabled=case.get("readonly_workers") is True,
        )
        case_settings = replace(
            case_settings, max_steps=min(settings.max_steps, 12),
            run_token_limit=min(settings.run_token_limit or 20_000, 20_000),
            model_timeout=min(settings.model_timeout, case_timeout),
        )
        if case.get("rag_index") is True:
            case_settings = replace(
                case_settings, embedding_provider="hash", embedding_model=None,
                embedding_base_url=None, embedding_api_key=None, embedding_dim=256,
            )
            IndexStore(sandbox).build()
        stage = "runtime"
        provider = (build_provider(case_settings) if live
                    else ScriptedProvider(case.get("script", [])))
        store = RuntimeStore(case_settings.state_dir / "evals" / f"{mode}.db")
        runtime = RuntimeHost(
            settings=case_settings, provider=provider, store=store, enable_mcp=False,
        )
        for price in prices:
            runtime.register_price(price)
        session_id = f"eval_{mode}_{index}"
        remaining = case_timeout - (time.perf_counter() - started)
        if remaining <= 0:
            raise TimeoutError("Evaluation fixture preparation exceeded its wall-clock limit")
        result, verification, acceptance_reasons = await _bounded_case(
            runtime, case, sandbox, session_id, remaining,
        )
        stage = "grading"
        _, reasons = _judge(case, result.output, store.get_events(result.run_id),
                           result.status, sandbox)
        reasons += acceptance_reasons
    except asyncio.CancelledError as exc:
        cancelled = True
        failure = safe_failure(exc, stage, settings)
        reasons += ("evaluation cancelled",)
    except Exception as exc:
        failure = safe_failure(exc, stage, settings)
        reasons += (f"{stage} failed: {type(exc).__name__}",)
    finally:
        if runtime is not None:
            cleanup = asyncio.create_task(runtime.close())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancelled = True
                    failure = failure or safe_failure(exc, "cleanup", settings)
                    reasons += ("evaluation cancelled",)
                except Exception:
                    break
            try:
                cleanup.result()
            except (Exception, asyncio.CancelledError) as exc:
                failure = failure or safe_failure(exc, "cleanup", settings)
                reasons += ("runtime cleanup failed",)
        if store is not None:
            try:
                evidence = case_evidence(
                    store, directory, main_run_id=result.run_id if result else None,
                )
            except Exception as exc:
                failure = failure or safe_failure(exc, "evidence", settings)
                reasons += ("event evidence export failed",)
            finally:
                store.close()
    status = ("cancelled" if cancelled else "timed_out"
              if failure and failure["type"] == "TimeoutError" else "failed"
              if failure else result.status if result else "unknown")
    if status not in {"completed", "interrupted"} and not reasons:
        reasons += (f"status={status}",)
    metrics = evidence.get("tool_metrics", {})
    main_run_id = evidence.get("main_run_id")
    main_identity: dict[str, Any] = next((run for run in evidence.get("runs", [])
                          if run["run_id"] == main_run_id), {})
    record = EvalCaseResult(
        id=str(case.get("id", f"case_{index}")), passed=not reasons,
        output=result.output if result else "", duration_ms=(time.perf_counter() - started) * 1000,
        tool_calls=metrics.get("calls", 0), tool_errors=metrics.get("errors", 0),
        safety_blocks=metrics.get("safety_blocks", 0),
        reasons=tuple(dict.fromkeys(reasons)), steps=result.steps if result else 0,
        usage=dict(evidence.get("usage", {}).get("reported_totals", {})), status=status,
        verification=verification, artifact_dir=str(sandbox) if sandbox else None,
        benchmark=dict(case.get("benchmark", {})), evidence=evidence, failure=failure,
        run_id=result.run_id if result else main_run_id,
        session_id=result.session_id if result else main_identity.get("session_id"),
        turn_id=result.turn_id if result else main_identity.get("turn_id"),
        configuration=safe_configuration(case_settings, live=live),
        case_artifact_dir=str(directory), agent_duration_ms=result.duration_ms if result else None,
    )
    try:
        atomic_json(directory / "case-result.json", asdict(record))
    except Exception as exc:
        record = replace(
            record, passed=False, status="cancelled" if cancelled else "failed",
            failure=record.failure or safe_failure(exc, "evidence", settings),
            reasons=(*record.reasons, "case result export failed"),
        )
    return record, cancelled


def _secrets(settings: Settings) -> tuple[str, ...]:
    return tuple(value for value in (settings.api_key, settings.embedding_api_key) if value)


def _report(
    path: Path, mode: str, directory: Path, results: list[EvalCaseResult], *,
    started: float, total: int, status: str, provenance: dict[str, Any],
) -> dict[str, Any]:
    passed = sum(item.passed for item in results)
    total_tools = sum(item.tool_calls for item in results)
    errors = sum(item.tool_errors for item in results)
    blocks = sum(item.safety_blocks for item in results)
    attempted_tools = total_tools - blocks
    failure_reasons: dict[str, int] = {}
    for item in results:
        for reason in item.reasons:
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
    usage = summarize_usage(results)
    report: dict[str, Any] = {
        "schema_version": 2, "status": status, "mode": mode,
        "measurement_kind": "provider_experiment" if mode == "live"
        else "scripted_mechanism_validation", "billing_applicable": mode == "live",
        "suite": str(path), "artifact_dir": str(directory),
        "report_path": str(directory / f"{mode}-report.json"),
        "passed": passed, "total": total, "attempted": len(results),
        "remaining": total - len(results), "provenance": provenance,
        "pass_rate": round(passed / total, 4) if total else 0,
        "task_completion_rate": round(passed / total, 4) if total else 0,
        "average_duration_ms": round(sum(item.duration_ms for item in results) / len(results), 2)
        if results else 0,
        "tool_success_rate": round((attempted_tools - errors) / attempted_tools, 4)
        if attempted_tools else 1.0,
        "safety_blocks": blocks, "failure_reasons": failure_reasons,
        "average_steps": round(sum(item.steps for item in results) / len(results), 2)
        if results else 0,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "usage": usage, "results": [asdict(item) for item in results],
    }
    if mode == "live":
        report["total_input_tokens"] = usage["reported_totals"].get("input_tokens")
        report["total_output_tokens"] = usage["reported_totals"].get("output_tokens")
    else:
        report["regression_smoke"] = True
    return report


async def run_evaluations(
    path: Path, *, live: bool = False, settings: Settings | None = None,
    output_dir: Path | None = None, prices: tuple[PriceVersion, ...] = (),
    case_timeout: float = 300, split: str | None = None,
    cases_snapshot: list[dict[str, Any]] | None = None,
    suite_metadata: dict[str, Any] | None = None,
    artifact_parent: Path | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run isolated cases; retain canonical evidence and incremental reports on every outcome.

    Frozen benchmark live is deliberately unavailable until shared scheduling and
    budget ownership are integrated. Prices are explicit immutable experiment inputs;
    this function never reads or copies a caller's budget/price ledger.
    """
    if isinstance(case_timeout, bool) or not 0 < case_timeout <= 300:
        raise ValueError("case_timeout must be positive and at most 300 seconds")
    # A trusted controller freezes validated JSON at admission. Never re-read a
    # mutable path when dispatching a queued batch. This is not an HTTP input.
    cases = (load_suite(path) if cases_snapshot is None
             else json.loads(json.dumps(cases_snapshot)))
    if cases_snapshot is not None and (live or settings is None or suite_metadata is None):
        raise ValueError("Frozen controlled evaluation requires explicit offline settings/metadata")
    benchmark = any("benchmark" in case for case in cases)
    if live and benchmark:
        raise ValueError(
            "Frozen benchmark live experiments are not enabled; use offline validation"
        )
    if split is not None:
        if split not in {"development", "holdout"} or not benchmark:
            raise ValueError("split requires a frozen benchmark and development or holdout")
        cases = [case for case in cases if case["benchmark"]["split"] == split]
    for price in prices:
        price.payload()
    settings = settings or Settings.from_env()
    if live:
        settings = resolve_provider_configuration(settings)
    else:
        settings = replace(
            settings, api_key=None, embedding_provider="hash", embedding_model=None,
            embedding_base_url=None, embedding_api_key=None, embedding_dim=256,
        )
    mode = "live" if live else "offline"
    if artifact_parent is not None:
        artifact_parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(
        prefix=f"eventide-evaluation-{mode}-", dir=artifact_parent,
    )).resolve()
    inputs = atomic_json(directory / "suite-inputs.json", normalize(cases, _secrets(settings)))
    provenance = {
        "runtime": runtime_revision(),
        "suite_source_sha256": (suite_metadata["manifest_sha256"] if suite_metadata is not None
                                else hashlib.sha256(path.read_bytes()).hexdigest()),
        "inputs": inputs, "split": split, "prices": [price.payload() for price in prices],
        "case_timeout": case_timeout,
        **({"builtin_suite": suite_metadata} if suite_metadata is not None else {}),
    }
    results: list[EvalCaseResult] = []
    started = time.perf_counter()

    def save(status: str) -> dict[str, Any]:
        report = _report(path, mode, directory, results, started=started, total=len(cases),
                         status=status, provenance=provenance)
        atomic_json(Path(report["report_path"]), report)
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            atomic_json(output_dir / f"{mode}-report.json", report)
        if progress is not None:
            progress("report_saved", report)
        return report

    save("running")
    for index, case in enumerate(cases, 1):
        if progress is not None:
            progress("case_started", {"id": case["id"], "index": index})
        entry, cancelled = await _evaluate_case(
            case, index, settings, directory / f"case-{index:03d}",
            live=live, prices=prices, case_timeout=case_timeout,
        )
        results.append(entry)
        if progress is not None:
            progress("case_completed", asdict(entry))
        if cancelled:
            save("cancelled")
            raise asyncio.CancelledError
        if live and entry.status in {"failed", "timed_out", "unknown"}:
            # Stop enlarging a paid batch after an unclassified provider/protocol failure.
            return save("aborted")
        save("running")
    return save("completed")
