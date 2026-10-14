"""MCP discovery and calls over official SDK transports."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_mcp_name(name: str) -> str:
    """Replace non [a-zA-Z0-9_-] characters with underscore."""
    return _DISALLOWED_CHARS.sub("_", name)


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> MCPServerConfig:
        transport = str(data.get("transport", "stdio")).replace("_", "-").lower()
        if transport not in {"stdio", "streamable-http"}:
            raise ValueError(f"MCP server '{name}' has unsupported transport '{transport}'")
        if transport == "stdio" and not data.get("command"):
            raise ValueError(f"MCP stdio server '{name}' requires command")
        if transport == "streamable-http" and not data.get("url"):
            raise ValueError(f"MCP HTTP server '{name}' requires url")
        return cls(
            name=name,
            transport=transport,
            command=data.get("command"),
            args=tuple(str(item) for item in data.get("args", [])),
            url=data.get("url"),
            env=tuple(str(item) for item in data.get("env", [])),
            headers={str(k): str(v) for k, v in data.get("headers", {}).items()},
        )


class MCPManager:
    """Own real MCP sessions and expose their tools to RuntimeHost."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.readonly_tools: set[str] = set()

    @staticmethod
    def load_configs(path: Path) -> list[MCPServerConfig]:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("servers", data)
        if not isinstance(servers, dict):
            raise ValueError("MCP config must contain an object named 'servers'")
        return [MCPServerConfig.from_dict(name, item) for name, item in servers.items()]

    async def connect_all(
        self, configs: list[MCPServerConfig], *, timeout: float | None = None
    ) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        for config in configs:
            try:
                if timeout is None:
                    await self.connect(config)
                else:
                    await asyncio.wait_for(self.connect(config, timeout=timeout), timeout)
            except Exception as exc:
                failures.append(
                    {
                        "server": config.name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return failures

    async def connect(
        self, config: MCPServerConfig, *, timeout: float | None = None
    ) -> None:
        try:
            from mcp import Client, StdioServerParameters
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install the 'mcp' package to use real MCP servers") from exc
        if config.name in self._sessions:
            return
        if config.transport == "stdio":
            selected_env = {key: os.environ[key] for key in config.env if key in os.environ}
            params = StdioServerParameters(
                command=str(config.command), args=list(config.args), env=selected_env or None
            )
            client_context = Client(params)
        else:
            import httpx2

            http_client = await self._stack.enter_async_context(
                httpx2.AsyncClient(headers=config.headers or None)
            )
            transport = streamable_http_client(str(config.url), http_client=http_client)
            client_context = Client(transport)
        session = await self._stack.enter_async_context(client_context)
        self._sessions[config.name] = session
        discovered = await session.list_tools()
        for tool in discovered.tools:
            safe_name = f"mcp__{normalize_mcp_name(config.name)}__{normalize_mcp_name(tool.name)}"
            annotations = getattr(tool, "annotations", None)
            annotation_data = (
                annotations.model_dump()
                if annotations is not None and hasattr(annotations, "model_dump")
                else {}
            )
            input_schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            self.tools.append(
                {
                    "name": safe_name,
                    "description": tool.description or "",
                    "input_schema": input_schema or {"type": "object", "properties": {}},
                }
            )
            if annotation_data.get("readOnlyHint") or annotation_data.get("read_only_hint"):
                self.readonly_tools.add(safe_name)

            async def invoke(
                *,
                _session: Any = session,
                _tool_name: str = tool.name,
                _timeout: float | None = timeout,
                **kwargs: Any,
            ) -> str:
                request = _session.call_tool(_tool_name, arguments=kwargs)
                result = (
                    await request
                    if _timeout is None
                    else await asyncio.wait_for(request, _timeout)
                )
                parts: list[str] = []
                for block in result.content:
                    text = getattr(block, "text", None)
                    if text is not None:
                        parts.append(str(text))
                    else:
                        parts.append(str(block))
                structured = getattr(result, "structured_content", None) or getattr(
                    result, "structuredContent", None
                )
                if structured:
                    parts.append(json.dumps(structured, ensure_ascii=False))
                is_error = getattr(result, "is_error", False) or getattr(result, "isError", False)
                prefix = "Error: " if is_error else ""
                return prefix + "\n".join(parts)

            self.handlers[safe_name] = invoke

    async def close(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()
