"""Shared deterministic providers, Git fixtures and port reporting for Vue E2E."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import uvicorn

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


class E2EUvicornServer(uvicorn.Server):
    """uvicorn.Server that announces its bound port once startup finishes."""

    async def startup(self, *args: Any, **kwargs: Any) -> None:
        await super().startup(*args, **kwargs)
        sock = self.servers[0].sockets[0]
        print(f"E2E_PORT={sock.getsockname()[1]}", flush=True)
