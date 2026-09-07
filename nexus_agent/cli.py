"""Command-line entry points for chat, one-shot runs, evals, and the API."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from nexus_agent.config import Settings
from nexus_agent.evaluation import run_evaluations
from nexus_agent.models import RunRequest, ToolCall
from nexus_agent.runtime import AgentRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-agent",
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
    return parser


async def _terminal_approval(_approval_id: str, call: ToolCall, reason: str) -> bool:
    print(f"\n[approval required] {call.name}: {reason}")
    print(json.dumps(call.arguments, ensure_ascii=False, indent=2))
    answer = await asyncio.to_thread(input, "Allow once? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


async def _run_once(args: argparse.Namespace) -> int:
    runtime = AgentRuntime(Settings.from_env())
    try:
        result = await runtime.run(RunRequest(args.prompt), approval_handler=_terminal_approval)
        print(
            json.dumps(asdict(result), ensure_ascii=False, indent=2) if args.json else result.output
        )
        return 0 if result.status == "completed" else 1
    finally:
        await runtime.close()


async def _chat() -> int:
    settings = Settings.from_env()
    runtime = AgentRuntime(settings)
    session_id = runtime.create_session()
    print("Nexus Agent · policy-aware runtime")
    print("Type q to quit. Runtime traces are stored under .nexus/.\n")
    try:
        while True:
            try:
                prompt = (await asyncio.to_thread(input, "nexus >> ")).strip()
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "chat"}:
        return asyncio.run(_chat())
    if args.command == "run":
        return asyncio.run(_run_once(args))
    if args.command == "eval":
        return asyncio.run(_eval(args))
    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as exc:
            parser.error("Install the 'web' dependencies to use serve")
            raise AssertionError from exc
        uvicorn.run("nexus_agent.api:create_app", factory=True, host=args.host, port=args.port)
        return 0
    parser.print_help()
    return 0
