"""Command-line entry points for chat, one-shot runs, evals, and the API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from eventide.config import Settings
from eventide.evaluation import run_evaluations
from eventide.migration import import_v02_database
from eventide.models import RunRequest, ToolCall
from eventide.runtime import AgentRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eventide",
        description="A traceable, policy-aware runtime for tool-using AI agents.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("chat", help="Start an interactive terminal session")
    run = subparsers.add_parser("run", help="Run one prompt and exit")
    run.add_argument("prompt", help="Task for the agent")
    run.add_argument("--json", action="store_true", help="Print structured RunResult JSON")
    evaluate = subparsers.add_parser("eval", help="Run an offline or live evaluation suite")
    evaluate.add_argument("suite", type=Path, help="YAML evaluation suite")
    evaluate.add_argument("--live", action="store_true", help="Use the configured live provider")
    evaluate.add_argument(
        "--json", action="store_true", help="Print the full report JSON instead of the summary"
    )
    serve = subparsers.add_parser("serve", help="Run the FastAPI service and Web console")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    resume = subparsers.add_parser("continue", help="Continue an interrupted session safely")
    resume.add_argument("session_id")
    resume.add_argument("--json", action="store_true")
    abandon = subparsers.add_parser(
        "abandon", help="Abandon recovery and allow new work in a parked session"
    )
    abandon.add_argument("session_id")
    abandon.add_argument("--json", action="store_true")
    migrate = subparsers.add_parser(
        "migrate-v02", help="Import a v0.2 nexus.db without modifying the source"
    )
    migrate.add_argument("source", nargs="?", type=Path)
    migrate.add_argument("--workspace", help="Workspace ID or existing path")
    export = subparsers.add_parser("export", help="Export one Run as canonical JSONL")
    export.add_argument("run_id")
    export.add_argument("destination", type=Path)
    export.add_argument("--force", action="store_true", help="Replace an existing file")
    plan = subparsers.add_parser("plan", help="Show the read-only task plan of a session")
    plan_actions = plan.add_subparsers(dest="plan_action", required=True)
    plan_show = plan_actions.add_parser(
        "show", help="Show a session's current task plan, active task, and evidence"
    )
    plan_show.add_argument("session_id")
    plan_show.add_argument("--json", action="store_true")
    workspace = subparsers.add_parser("workspace", help="Manage registered project directories")
    actions = workspace.add_subparsers(dest="workspace_action", required=True)
    actions.add_parser("list")
    actions.add_parser("add").add_argument("target")
    actions.add_parser("show").add_argument("target")
    actions.add_parser("remove").add_argument("target")
    for name in ("run", "chat", "serve"):
        subparsers.choices[name].add_argument("--workspace", help="Workspace ID or existing path")
    for name in ("run", "chat"):
        subparsers.choices[name].add_argument(
            "--cwd", help="Session working directory inside the Workspace"
        )
    return parser


async def _terminal_approval(_approval_id: str, call: ToolCall, reason: str) -> bool:
    print(f"\n[approval required] {call.name}: {reason}")
    print(json.dumps(call.arguments, ensure_ascii=False, indent=2))
    answer = await asyncio.to_thread(input, "Allow once? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


async def _run_once(args: argparse.Namespace) -> int:
    runtime = AgentRuntime(Settings.from_env())
    try:
        workspace = (
            runtime.resolve_or_register_workspace(args.workspace) if args.workspace else None
        )
        inferred_cwd = (
            args.workspace
            if args.workspace and not args.workspace.startswith("ws_")
            else None
        )
        session = runtime.create_session(
            workspace_id=workspace.workspace_id if workspace else None,
            working_directory=args.cwd or inferred_cwd,
        )
        result = await runtime.run(
            RunRequest(args.prompt, session), approval_handler=_terminal_approval
        )
        print(
            json.dumps(asdict(result), ensure_ascii=False, indent=2) if args.json else result.output
        )
        return 0 if result.status == "completed" else 1
    finally:
        await runtime.close()


async def _chat(
    workspace_target: str | None = None,
    working_directory: str | None = None,
) -> int:
    settings = Settings.from_env()
    runtime = AgentRuntime(settings)
    try:
        workspace = (
            runtime.resolve_or_register_workspace(workspace_target) if workspace_target else None
        )
        session_id = runtime.create_session(
            workspace_id=workspace.workspace_id if workspace else None,
            working_directory=working_directory
            or (
                workspace_target
                if workspace_target and not workspace_target.startswith("ws_")
                else None
            ),
        )
        print("Eventide · policy-aware runtime")
        print(f"Type q to quit. State: {settings.state_dir}. Session: {session_id}\n")
        while True:
            try:
                prompt = (await asyncio.to_thread(input, "eventide >> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt.lower() in {"", "q", "quit", "exit"}:
                break
            result = await runtime.run(
                RunRequest(prompt, session_id=session_id), approval_handler=_terminal_approval
            )
            print(result.output, "\n")
        return 0
    finally:
        await runtime.close()


def _format_eval_summary(report: dict[str, Any]) -> str:
    """Compact human-readable eval view; exit codes stay pass/fail only."""
    lines = [f"评测套件：{report.get('suite', '?')}"]
    if report.get("mode") == "live":
        lines.append(
            "模式：live · "
            f"输入 tokens {report.get('total_input_tokens', 0)} · "
            f"输出 tokens {report.get('total_output_tokens', 0)} · "
            f"总耗时 {report.get('duration_ms', 0)} ms"
        )
    else:
        lines.append("模式：offline · scripted 回归冒烟，不代表真实模型完成率")
    passed, total = report.get("passed", 0), report.get("total", 0)
    rate = report.get("task_completion_rate", report.get("pass_rate", 0))
    lines.append(f"任务完成率：{passed}/{total}（{rate * 100:.1f}%）")
    lines.append(
        f"平均步数：{report.get('average_steps', 0)} · "
        f"工具成功率：{report.get('tool_success_rate', 1.0)} · "
        f"安全拦截：{report.get('safety_blocks', 0)}"
    )
    failures = report.get("failure_reasons") or {}
    if failures:
        lines.append("失败原因分布：")
        for reason, count in sorted(failures.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"  - {reason} × {count}")
        lines.append("失败用例：")
        for item in report.get("results", []):
            if not item.get("passed"):
                lines.append(f"  - {item.get('id')}: {'; '.join(item.get('reasons', []))}")
    return "\n".join(lines)


async def _eval(args: argparse.Namespace) -> int:
    report = await run_evaluations(args.suite, live=args.live)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_eval_summary(report))
    return 0 if report["passed"] == report["total"] else 1


def _render_plan_text(status: dict[str, Any]) -> str:
    """Human-readable plan view over session_status facts; no editing, no new state."""
    lines: list[str] = [f"Session: {status['session_id']} ({status['title']})"]
    tasks = status.get("task_state", {}).get("tasks", [])
    if not tasks:
        lines.append("任务计划：尚无计划。只有模型调用 todo_write 才会创建计划。")
        return "\n".join(lines)
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.get("status", "pending")] = counts.get(task.get("status", "pending"), 0) + 1
    status_label = {
        "pending": "pending",
        "in_progress": "in_progress",
        "completed": "completed",
        "blocked": "blocked",
    }
    lines.append(
        "任务计划：{total} 项；completed {completed} · in_progress {active} · "
        "pending {pending} · blocked {blocked}".format(
            total=len(tasks),
            completed=counts.get("completed", 0),
            active=counts.get("in_progress", 0),
            pending=counts.get("pending", 0),
            blocked=counts.get("blocked", 0),
        )
    )
    active = status.get("active_task_id")
    for task in tasks:
        task_status = task.get("status", "pending")
        marker = status_label.get(task_status, task_status)
        if task.get("id") and task.get("id") == active:
            marker = f"{marker} · active"
        lines.append(f"[{marker}] {task.get('id') or '-'} {task.get('content', '')}")
        summary = task.get("summary")
        if summary:
            lines.append(f"  summary: {summary}")
        for entry in task.get("evidence", []):
            outcome = "error" if entry.get("is_error") else "ok"
            lines.append(
                f"  evidence: {entry.get('name')} {outcome} "
                f"(run {entry.get('run_id')} call {entry.get('call_id')})"
            )
        omitted = task.get("evidence_omitted")
        if omitted:
            lines.append(f"  evidence: …and {omitted} more")
    lines.append(
        "注：completed 与 evidence 只说明发生过什么，不代表验证通过；"
        "计划只读，更新只能由模型通过 todo_write 提交。"
    )
    return "\n".join(lines)


async def _manage(args: argparse.Namespace) -> int:
    runtime = AgentRuntime(Settings.from_env())
    try:
        value: Any
        if args.command in {"continue", "abandon"}:
            result = (
                await runtime.continue_session(
                    args.session_id, approval_handler=_terminal_approval
                )
                if args.command == "continue"
                else await runtime.abandon_interruption(args.session_id)
            )
            print(json.dumps(asdict(result), ensure_ascii=False) if args.json else result.output)
            return 0 if result.status == "completed" else 1
        if args.command == "migrate-v02":
            workspace = (
                runtime.resolve_or_register_workspace(args.workspace)
                if args.workspace
                else runtime.default_workspace
            )
            source = args.source or (Path(workspace.path) / ".nexus" / "nexus.db")
            value = import_v02_database(runtime.store, source, workspace.workspace_id)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "plan":
            status = runtime.session_status(args.session_id)
            if args.json:
                print(
                    json.dumps(
                        {
                            "session_id": status["session_id"],
                            "task_plan": status["task_plan"],
                            "active_task_id": status["active_task_id"],
                            "task_state": status["task_state"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(_render_plan_text(status))
            return 0
        if args.command == "export":
            if runtime.store.get_run_identity(args.run_id) is None:
                raise ValueError(f"Run not found: {args.run_id}")
            destination = args.destination.expanduser().resolve()
            if destination.exists() and not args.force:
                raise ValueError(f"Destination already exists: {destination}; use --force")
            runtime.store.export_jsonl(args.run_id, destination)
            print(json.dumps({"run_id": args.run_id, "destination": str(destination)}))
            return 0
        if args.workspace_action == "list":
            value = [asdict(w) for w in runtime.store.list_workspaces()]
        elif args.workspace_action == "remove":
            runtime.store.remove_workspace(args.target)
            value = {"removed": args.target, "files_deleted": False}
        else:
            record = (
                runtime.store.get_workspace(args.target)
                if args.workspace_action == "show"
                else runtime.resolve_or_register_workspace(args.target)
            )
            value = asdict(record)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        print(str(exc))
        return 1
    finally:
        await runtime.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "chat"}:
        return asyncio.run(
            _chat(
                getattr(args, "workspace", None),
                getattr(args, "cwd", None),
            )
        )
    if args.command == "run":
        return asyncio.run(_run_once(args))
    if args.command == "eval":
        return asyncio.run(_eval(args))
    if args.command in {
        "workspace",
        "continue",
        "abandon",
        "migrate-v02",
        "export",
        "plan",
    }:
        return asyncio.run(_manage(args))
    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as exc:
            parser.error("Install the 'web' dependencies to use serve")
            raise AssertionError from exc
        from eventide.api import create_app

        runtime = AgentRuntime(Settings.from_env())
        try:
            if args.workspace:
                runtime.default_workspace = runtime.resolve_or_register_workspace(args.workspace)
            uvicorn.run(create_app(runtime), host=args.host, port=args.port)
        finally:
            asyncio.run(runtime.close())
        return 0
    parser.print_help()
    return 0
