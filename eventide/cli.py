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
    workspace = subparsers.add_parser("workspace", help="Manage registered project directories")
    actions = workspace.add_subparsers(dest="workspace_action", required=True)
    actions.add_parser("list")
    actions.add_parser("add").add_argument("target")
    actions.add_parser("show").add_argument("target")
    actions.add_parser("remove").add_argument("target")
    for name in ("run", "chat", "serve"):
        subparsers.choices[name].add_argument("--workspace", help="Workspace ID or existing path")
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
        session = runtime.create_session(workspace_id=workspace.workspace_id if workspace else None)
        result = await runtime.run(
            RunRequest(args.prompt, session), approval_handler=_terminal_approval
        )
        print(
            json.dumps(asdict(result), ensure_ascii=False, indent=2) if args.json else result.output
        )
        return 0 if result.status == "completed" else 1
    finally:
        await runtime.close()


async def _chat(workspace_target: str | None = None) -> int:
    settings = Settings.from_env()
    runtime = AgentRuntime(settings)
    try:
        workspace = (
            runtime.resolve_or_register_workspace(workspace_target) if workspace_target else None
        )
        session_id = runtime.create_session(
            workspace_id=workspace.workspace_id if workspace else None
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


async def _eval(args: argparse.Namespace) -> int:
    report = await run_evaluations(args.suite, live=args.live)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] == report["total"] else 1


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
        return asyncio.run(_chat(getattr(args, "workspace", None)))
    if args.command == "run":
        return asyncio.run(_run_once(args))
    if args.command == "eval":
        return asyncio.run(_eval(args))
    if args.command in {"workspace", "continue", "abandon", "migrate-v02"}:
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
