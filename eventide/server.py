"""Deployment entry point for the one Web workbench service."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

from eventide.api import create_app
from eventide.config import Settings
from eventide.host import RuntimeHost


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eventide-server",
        description="Start the Eventide Web workbench service.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workspace", type=Path, help="Initial project directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    runtime = None
    try:
        settings = (
            Settings.from_env(workdir=args.workspace) if args.workspace else Settings.from_env()
        )
        runtime = RuntimeHost(settings)
        uvicorn.run(create_app(runtime), host=args.host, port=args.port, proxy_headers=False)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Eventide service failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            asyncio.run(runtime.close())
