"""Standalone real-server harness for the Web E2E suite.

Boots the production FastAPI app (``eventide.api.create_app``) on uvicorn with a
content-routed fake provider, so ``tests/web_e2e.cjs`` can drive real HTTP
sessions without any model API key or network access.

Usage::

    python tests/e2e_server.py [--port 0]

Lifecycle contract with the Node driver:

- Once the HTTP socket is bound, stdout receives ``E2E_WORKSPACE=<path>``
  (the main Git fixture) followed by ``E2E_PORT=<port>``.
- ``POST /__e2e/shutdown`` asks the server to exit gracefully; SIGINT/SIGTERM
  do the same. Temporary state and workspace fixtures are removed on exit.

FakeProvider routing table (matched against the last string user message):

- ``多步任务``: todo_write(3 items, 1 in_progress) -> read_file(README.md) ->
  bash(echo ok) -> todo_write(all completed with summaries) -> final text
  ``多步任务已完成``.
- ``审批场景``: bash that triggers the ASK policy (``del scratch.txt`` on
  Windows / ``rm scratch.txt`` elsewhere) -> after the decision, final text
  ``审批场景已完成``.
- ``停驻场景``: read_file forever; the run parks on the step budget, and the
  first request of a Continue run (per-session counter) returns ``已恢复完成``.
- ``附件场景``: final text ``附件已读取`` (used by attachment assertions).
- anything else: plain text reply.

EVENTIDE_MAX_STEPS is 6, not 2: the five-step multi-step scenario must fit
inside one run budget, and the park scenario still parks on ``step_budget``
after six read_file steps. Keep the texts in sync with tests/web_e2e.cjs.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import uvicorn

MAX_STEPS = 6
APPROVAL_TIMEOUT = 60.0
STATE_ROOT_PREFIX = "eventide-e2e-"

PLAN_INITIAL = (
    {"content": "阅读 README 了解项目", "status": "in_progress"},
    {"content": "查看 src/demo.py 源码", "status": "pending"},
    {"content": "运行演示命令验证", "status": "pending"},
)
# Same contents as PLAN_INITIAL so plan_update reuses the identities t1-t3
# without the fake provider having to know the assigned ids.
PLAN_FINISHED = (
    {
        "content": "阅读 README 了解项目",
        "status": "completed",
        "summary": "已读取 README.md",
    },
    {
        "content": "查看 src/demo.py 源码",
        "status": "completed",
        "summary": "已读取 src/demo.py",
    },
    {
        "content": "运行演示命令验证",
        "status": "completed",
        "summary": "已执行 bash echo ok",
    },
)


class FakeProvider:
    """Content-routed deterministic provider with per-session request counts."""

    def __init__(self, max_steps: int) -> None:
        self.max_steps = max_steps
        self.counts: dict[str, int] = {}
        self.requests: list[Any] = []
        self._runtime: Any = None

    def attach(self, runtime: Any) -> None:
        # The provider only sees ModelRequest, so it recovers the session by
        # mapping the current task back to its run through the owning host.
        self._runtime = runtime

    def _session_key(self) -> str:
        task = asyncio.current_task()
        runtime = self._runtime
        if task is not None and runtime is not None:
            for run_id, owner in list(runtime._run_tasks.items()):
                if owner is task:
                    record = runtime.store.get_run(run_id)
                    if record:
                        return str(record["session_id"])
        return "anonymous"

    @staticmethod
    def _prompt(request: Any) -> str:
        for message in reversed(request.messages):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return str(message["content"])
        return ""

    @staticmethod
    def _call(name: str, arguments: dict[str, Any]) -> Any:
        from eventide.models import ToolCall

        return ToolCall(f"call_{uuid.uuid4().hex[:12]}", name, arguments)

    def _response(self, text: str, usage: dict[str, int]) -> Any:
        from eventide.models import ModelResponse

        return ModelResponse(text=text, usage=usage)

    def _tool_response(self, calls: tuple[Any, ...], usage: dict[str, int]) -> Any:
        from eventide.models import ModelResponse

        return ModelResponse(tool_calls=calls, stop_reason="tool_use", usage=usage)

    async def complete(self, request: Any) -> Any:
        self.requests.append(request)
        step = self.counts[self._session_key()] = self.counts.get(self._session_key(), 0) + 1
        prompt = self._prompt(request)
        usage = {"input_tokens": 300 + 20 * step, "output_tokens": 24 + 8 * step}
        if "多步任务" in prompt:
            return self._multi_step(step, usage)
        if "审批场景" in prompt:
            return self._approval(step, usage)
        if "停驻场景" in prompt:
            return self._park(step, usage)
        if "附件场景" in prompt:
            return self._response("附件已读取", usage)
        return self._response(f"默认文本回复：{prompt[:48]}", usage)

    def _multi_step(self, step: int, usage: dict[str, int]) -> Any:
        if step == 1:
            todos = [dict(item) for item in PLAN_INITIAL]
            return self._tool_response((self._call("todo_write", {"todos": todos}),), usage)
        if step == 2:
            return self._tool_response((self._call("read_file", {"path": "README.md"}),), usage)
        if step == 3:
            return self._tool_response((self._call("bash", {"command": "echo ok"}),), usage)
        if step == 4:
            todos = [dict(item) for item in PLAN_FINISHED]
            return self._tool_response((self._call("todo_write", {"todos": todos}),), usage)
        return self._response("多步任务已完成", usage)

    def _approval(self, step: int, usage: dict[str, int]) -> Any:
        if step > 1:
            return self._response("审批场景已完成", usage)
        # Neither branch is "echo": the ASK policy only reviews destructive
        # commands, so the fixture carries scratch.txt as the deletion target.
        command = "del scratch.txt" if os.name == "nt" else "rm scratch.txt"
        return self._tool_response((self._call("bash", {"command": command}),), usage)

    def _park(self, step: int, usage: dict[str, int]) -> Any:
        if step <= self.max_steps:
            return self._tool_response((self._call("read_file", {"path": "README.md"}),), usage)
        # The parking run consumed exactly max_steps requests, so the first
        # request after it can only come from a Continue run.
        return self._response("已恢复完成", usage)


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


def _write_workspace(root: Path, files: dict[str, str], *, commit_message: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    _run_git(root, "init")
    _run_git(root, "add", "-A")
    _run_git(
        root,
        "-c",
        "user.name=E2E Runner",
        "-c",
        "user.email=e2e@example.com",
        "commit",
        "-m",
        commit_message,
    )
    return root


def build_fixtures(base: Path, python_exe: str) -> dict[str, Path]:
    """Create three Git workspaces: main run target, plain, and capability-rich."""
    main = _write_workspace(
        base / "main",
        {
            "README.md": (
                "# E2E 演示工作区\n\n"
                "这是多步任务场景里 read_file 的目标文件。\n第二行内容。\n第三行内容。\n"
            ),
            "src/demo.py": '"""演示模块。"""\n\n\ndef main() -> None:\n    print("demo ok")\n',
            # Deletion target for the approval scenario; the ASK policy only
            # reviews destructive commands, never plain `echo`.
            "scratch.txt": "temporary scratch file\n",
        },
        commit_message="init main fixture",
    )
    plain = _write_workspace(
        base / "plain",
        {"README.md": "# 纯净工作区\n\n不包含 mcp.json，用于验证能力面板的空态。\n"},
        commit_message="init plain fixture",
    )
    caps = _write_workspace(
        base / "caps",
        {
            "README.md": "# 能力面板工作区\n\n包含 skills/demo 与 mcp.json。\n",
            "skills/demo/SKILL.md": (
                "---\n"
                "name: demo\n"
                "description: 演示技能，用于能力面板自省。\n"
                "---\n\n"
                "# Demo\n\n演示技能正文。\n"
            ),
            # Not required to be reachable: the capabilities endpoint parses
            # this file without connecting, and no run is scheduled here.
            "mcp.json": (
                json.dumps(
                    {
                        "servers": {
                            "docs": {
                                "transport": "stdio",
                                "command": python_exe,
                                "args": ["-c", "raise SystemExit(0)"],
                            }
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ),
        },
        commit_message="init caps fixture",
    )
    return {"main": main, "plain": plain, "caps": caps}


def _remove_tree(root: Path, *, deadline_s: float = 4.0) -> None:
    """rmtree that survives Windows read-only files and late-closing handles."""
    if not root.exists():
        return
    limit = time.monotonic() + deadline_s
    while True:
        try:
            shutil.rmtree(root)
            return
        except (PermissionError, OSError):
            if time.monotonic() >= limit:
                break
            time.sleep(0.25)
    for path in sorted(root.rglob("*"), reverse=True):
        with suppress(OSError):
            path.chmod(stat.S_IWRITE)
    shutil.rmtree(root, ignore_errors=True)


def _sweep_stale_roots(max_age_hours: float = 6.0) -> None:
    """Clean roots left behind by force-killed earlier harness runs."""
    cutoff = time.time() - max_age_hours * 3600
    for candidate in Path(tempfile.gettempdir()).glob(f"{STATE_ROOT_PREFIX}*"):
        try:
            if candidate.stat().st_mtime < cutoff:
                _remove_tree(candidate)
        except OSError:
            continue


class E2EUvicornServer(uvicorn.Server):
    """uvicorn.Server that announces its bound port once startup finishes."""

    async def startup(self, *args: Any, **kwargs: Any) -> None:
        await super().startup(*args, **kwargs)
        sock = self.servers[0].sockets[0]
        print(f"E2E_PORT={sock.getsockname()[1]}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eventide Web E2E server harness")
    parser.add_argument("--port", type=int, default=0, help="bind port; 0 picks a free port")
    args = parser.parse_args(argv)

    _sweep_stale_roots()
    temp_root = Path(tempfile.mkdtemp(prefix=STATE_ROOT_PREFIX))
    atexit.register(_remove_tree, temp_root)
    original_cwd = Path.cwd()
    # Keep python-dotenv discovery and any relative writes inside the sandbox.
    os.chdir(temp_root)

    fixtures = build_fixtures(temp_root / "workspaces", sys.executable)
    os.environ["EVENTIDE_STATE_DIR"] = str(temp_root / "state")
    os.environ["EVENTIDE_MAX_STEPS"] = str(MAX_STEPS)
    os.environ["EVENTIDE_APPROVAL_TIMEOUT"] = str(APPROVAL_TIMEOUT)
    for name in (
        "EVENTIDE_API_KEY",
        "ANTHROPIC_API_KEY",
        "EVENTIDE_BASE_URL",
        "ANTHROPIC_BASE_URL",
    ):
        os.environ.pop(name, None)

    # Imported after the environment above is in place (config reads env vars
    # and any .env at import time).
    from eventide.api import create_app
    from eventide.config import Settings
    from eventide.runtime import AgentRuntime

    settings = Settings.from_env(workdir=fixtures["main"])
    provider = FakeProvider(max_steps=settings.max_steps)
    runtime = AgentRuntime(settings, provider)
    provider.attach(runtime)
    # The UI refuses to submit while no model is configured; seed a dummy
    # configuration so the journey passes the isConfigured gate, then put the
    # fake provider back (configure_provider clears the live provider).
    asyncio.run(
        runtime.configure_provider(
            provider="openai_responses",
            model="e2e-fake-model",
            api_key="dummy-key",
            base_url=None,
            persist=True,
        )
    )
    runtime.provider = provider
    app = create_app(runtime)

    server = E2EUvicornServer(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
    )

    @app.post("/__e2e/shutdown")
    async def shutdown() -> dict[str, bool]:
        server.should_exit = True
        return {"ok": True}

    print(f"E2E_WORKSPACE={fixtures['main']}", flush=True)
    try:
        server.run()
    finally:
        # Windows cannot delete a directory that is any process's CWD.
        os.chdir(original_cwd)
        _remove_tree(temp_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
