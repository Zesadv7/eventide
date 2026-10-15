"""Deterministic offline and opt-in live evaluation runner."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.models import RunRequest, RunResult
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


def load_suite(path: Path) -> list[dict[str, Any]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("Evaluation suite must contain a 'cases' list")
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
    root and the repo-local git identity live inside the sandbox, so later
    commits (seeded or model-driven) need no global git configuration.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="eventide-eval-", dir=parent))
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
    request = RunRequest(prompt=str(case["prompt"]), session_id=session_id)
    if case.get("acceptance_tests") is not True:
        return await runtime.run(request), {}, ()
    if sandbox is None:
        raise ValueError("acceptance_tests requires an inline workspace fixture")
    original = _test_manifest(sandbox)
    runtime.create_session(session_id)
    with tempfile.TemporaryDirectory(prefix="eventide-fixed-tests-") as temporary:
        frozen = Path(temporary) / "tests"
        shutil.copytree(
            sandbox / "tests", frozen, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        command = _acceptance_command(sandbox, frozen)
        baseline = await runtime.check_project(session_id, command)
        if baseline["passed"] or baseline["metadata"].get("exit_code") != 1:
            raise ValueError("Acceptance fixture must have a failing baseline (exit code 1)")
        if "EVENTIDE_ACCEPTANCE=" not in baseline["content"]:
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
        }
        if not unchanged:
            return result, {**evidence, "passed": False}, ("acceptance tests changed",)
        _clear_candidate_bytecode(sandbox)
        checked = await runtime.check_project(session_id, command)
        unchanged = _test_manifest(sandbox) == original
        accepted = checked["passed"] and unchanged and "EVENTIDE_ACCEPTANCE=" in checked["content"]
        evidence.update(
            {
                "verification_run_id": checked["run_id"],
                "exit_code": checked["metadata"].get("exit_code"),
                "timed_out": checked["metadata"].get("timed_out", False),
                "tests_unchanged": unchanged,
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


async def run_evaluations(
    path: Path,
    *,
    live: bool = False,
    settings: Settings | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run cases in independent temporary projects, never in the caller's checkout.

    Live mode resolves saved provider settings read-only before isolation. The
    returned artifact_dir keeps all case databases and source evidence, including
    failed cases. output_dir explicitly exports the report only; it is not a
    state root and receives no credentials or runtime databases.
    """
    settings = settings or Settings.from_env()
    if live:
        settings = resolve_provider_configuration(settings)
    else:
        settings = replace(
            settings,
            api_key=None,
            embedding_provider="hash",
            embedding_model=None,
            embedding_base_url=None,
            embedding_api_key=None,
            embedding_dim=256,
        )
    cases = load_suite(path)
    mode = "live" if live else "offline"
    artifact_dir = Path(tempfile.mkdtemp(prefix=f"eventide-evaluation-{mode}-"))
    results: list[EvalCaseResult] = []
    suite_started = time.perf_counter()
    for index, case in enumerate(cases, 1):
        fixtures = case.get("workspace", {})
        if not isinstance(fixtures, dict):
            raise ValueError("Evaluation workspace must be an inline fixture mapping")
        sandbox = _create_sandbox(fixtures, parent=artifact_dir)
        case_settings = replace(
            settings,
            workdir=sandbox,
            state_dir=sandbox / ".eventide",
            workers_enabled=case.get("readonly_workers") is True,
        )
        rag_index = case.get("rag_index") is True
        if rag_index:
            # Fixture indexes are always deterministic, including live LLM runs.
            case_settings = replace(
                case_settings,
                embedding_provider="hash",
                embedding_model=None,
                embedding_base_url=None,
                embedding_api_key=None,
                embedding_dim=256,
            )
            IndexStore(sandbox).build()
        provider = (
            build_provider(case_settings) if live else ScriptedProvider(case.get("script", []))
        )
        case_store = RuntimeStore(case_settings.state_dir / "evals" / f"{mode}.db")
        runtime = None
        try:
            runtime = RuntimeHost(
                settings=case_settings, provider=provider, store=case_store, enable_mcp=False
            )
            session_id = f"eval_{mode}_{index}_{int(time.time() * 1000)}"
            result, verification, acceptance_reasons = await _run_case(
                runtime, case, sandbox, session_id
            )
            events = case_store.get_events(result.run_id)
            passed, reasons = _judge(case, result.output, events, result.status, sandbox)
            reasons += acceptance_reasons
            passed = passed and not acceptance_reasons
            error_events = [
                event
                for event in events
                if event["type"] == "tool.result" and event["payload"].get("is_error")
            ]
            safety_blocks = sum(
                "Permission denied" in str(event["payload"].get("content", ""))
                for event in error_events
            )
            results.append(
                EvalCaseResult(
                    id=str(case.get("id", f"case_{index}")),
                    passed=passed,
                    output=result.output,
                    duration_ms=result.duration_ms,
                    tool_calls=result.tool_calls,
                    tool_errors=len(error_events) - safety_blocks,
                    safety_blocks=safety_blocks,
                    reasons=reasons,
                    steps=result.steps,
                    usage=dict(result.usage),
                    status=result.status,
                    verification=verification,
                    artifact_dir=str(sandbox),
                )
            )
        finally:
            try:
                if runtime is not None:
                    await runtime.close()
            finally:
                case_store.close()
    passed_count = sum(item.passed for item in results)
    total_tools = sum(item.tool_calls for item in results)
    total_errors = sum(item.tool_errors for item in results)
    total_blocks = sum(item.safety_blocks for item in results)
    attempted_tools = total_tools - total_blocks
    failure_reasons: dict[str, int] = {}
    for item in results:
        for reason in item.reasons:
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
    report: dict[str, Any] = {
        "mode": mode,
        "suite": str(path),
        "artifact_dir": str(artifact_dir),
        "report_path": str(artifact_dir / f"{mode}-report.json"),
        "passed": passed_count,
        "total": len(results),
        "pass_rate": round(passed_count / len(results), 4) if results else 0,
        "task_completion_rate": round(passed_count / len(results), 4) if results else 0,
        "average_duration_ms": round(sum(item.duration_ms for item in results) / len(results), 2)
        if results
        else 0,
        "tool_success_rate": round((attempted_tools - total_errors) / attempted_tools, 4)
        if attempted_tools
        else 1.0,
        "safety_blocks": total_blocks,
        "failure_reasons": failure_reasons,
        "average_steps": round(sum(item.steps for item in results) / len(results), 2)
        if results
        else 0,
        "duration_ms": round((time.perf_counter() - suite_started) * 1000, 2),
        "results": [asdict(item) for item in results],
    }
    if live:
        report["total_input_tokens"] = sum(item.usage.get("input_tokens", 0) for item in results)
        report["total_output_tokens"] = sum(item.usage.get("output_tokens", 0) for item in results)
    else:
        report["regression_smoke"] = True
    body = json.dumps(report, ensure_ascii=False, indent=2)
    Path(report["report_path"]).write_text(body, encoding="utf-8")
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{mode}-report.json").write_text(body, encoding="utf-8")
    return report
