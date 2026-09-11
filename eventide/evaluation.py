"""Deterministic offline and opt-in live evaluation runner."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from eventide.config import Settings
from eventide.models import RunRequest
from eventide.observability import TraceStore
from eventide.providers import ScriptedProvider, build_provider
from eventide.runtime import AgentRuntime


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


def _create_sandbox(fixtures: dict[str, Any]) -> Path:
    """Materialize one case's fixtures in a throwaway Git workspace.

    The sandbox is deliberately not auto-deleted: failed cases keep their
    workspace, database, and git history around for debugging. The eval state
    root and the repo-local git identity live inside the sandbox, so later
    commits (seeded or model-driven) need no global git configuration.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="eventide-eval-"))
    for relative, content in fixtures.items():
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
    path: Path, *, live: bool = False, settings: Settings | None = None
) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    cases = load_suite(path)
    mode = "live" if live else "offline"
    database = settings.state_dir / "evals" / f"{mode}.db"
    suite_store = TraceStore(database)
    results: list[EvalCaseResult] = []
    suite_started = time.perf_counter()
    for index, case in enumerate(cases, 1):
        case_settings = settings
        case_store = suite_store
        sandbox: Path | None = None
        fixtures = case.get("workspace")
        if isinstance(fixtures, dict):
            sandbox = _create_sandbox(fixtures)
            case_settings = replace(settings, workdir=sandbox, state_dir=sandbox / ".eventide")
            case_store = TraceStore(case_settings.state_dir / "evals" / f"{mode}.db")
        provider = (
            build_provider(case_settings) if live else ScriptedProvider(case.get("script", []))
        )
        runtime = AgentRuntime(
            settings=case_settings, provider=provider, store=case_store, enable_mcp=live
        )
        session_id = f"eval_{mode}_{index}_{int(time.time() * 1000)}"
        result = await runtime.run(
            RunRequest(
                prompt=str(case["prompt"]),
                session_id=session_id,
            )
        )
        events = case_store.get_events(result.run_id)
        passed, reasons = _judge(case, result.output, events, result.status, sandbox)
        error_events = [
            event
            for event in events
            if event["type"] == "tool.result" and event["payload"].get("is_error")
        ]
        safety_blocks = sum(
            "Permission denied" in str(event["payload"].get("content", ""))
            for event in error_events
        )
        tool_errors = len(error_events) - safety_blocks
        results.append(
            EvalCaseResult(
                id=str(case.get("id", f"case_{index}")),
                passed=passed,
                output=result.output,
                duration_ms=result.duration_ms,
                tool_calls=result.tool_calls,
                tool_errors=tool_errors,
                safety_blocks=safety_blocks,
                reasons=reasons,
                steps=result.steps,
                usage=dict(result.usage),
                status=result.status,
            )
        )
        await runtime.close()
        if case_store is not suite_store:
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
    report_dir = settings.state_dir / "evals"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{mode}-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    suite_store.close()
    return report
