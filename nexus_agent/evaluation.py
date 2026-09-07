"""Deterministic offline and opt-in live evaluation runner."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nexus_agent.config import Settings
from nexus_agent.models import RunRequest
from nexus_agent.observability import TraceStore
from nexus_agent.providers import ScriptedProvider, build_provider
from nexus_agent.runtime import AgentRuntime


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


def load_suite(path: Path) -> list[dict[str, Any]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("Evaluation suite must contain a 'cases' list")
    return cases


def _judge(
    case: dict[str, Any], output: str, events: list[dict[str, Any]], status: str
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
    return not reasons, tuple(reasons)


async def run_evaluations(
    path: Path, *, live: bool = False, settings: Settings | None = None
) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    cases = load_suite(path)
    database = settings.state_dir / "evals" / ("live.db" if live else "offline.db")
    store = TraceStore(database)
    results: list[EvalCaseResult] = []
    suite_started = time.perf_counter()
    for index, case in enumerate(cases, 1):
        provider = build_provider(settings) if live else ScriptedProvider(case.get("script", []))
        runtime = AgentRuntime(settings=settings, provider=provider, store=store)
        mode = "live" if live else "offline"
        session_id = f"eval_{mode}_{index}_{int(time.time() * 1000)}"
        result = await runtime.run(
            RunRequest(
                prompt=str(case["prompt"]),
                session_id=session_id,
            )
        )
        events = store.get_events(result.run_id)
        passed, reasons = _judge(case, result.output, events, result.status)
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
            )
        )
        await runtime.close()
    passed_count = sum(item.passed for item in results)
    total_tools = sum(item.tool_calls for item in results)
    total_errors = sum(item.tool_errors for item in results)
    total_blocks = sum(item.safety_blocks for item in results)
    attempted_tools = total_tools - total_blocks
    report = {
        "mode": "live" if live else "offline",
        "suite": str(path),
        "passed": passed_count,
        "total": len(results),
        "pass_rate": round(passed_count / len(results), 4) if results else 0,
        "average_duration_ms": round(sum(item.duration_ms for item in results) / len(results), 2)
        if results
        else 0,
        "tool_success_rate": round((attempted_tools - total_errors) / attempted_tools, 4)
        if attempted_tools
        else 1.0,
        "safety_blocks": total_blocks,
        "duration_ms": round((time.perf_counter() - suite_started) * 1000, 2),
        "results": [asdict(item) for item in results],
    }
    report_dir = settings.state_dir / "evals"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{report['mode']}-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    store.close()
    return report
