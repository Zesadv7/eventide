"""Real stdio integration test using the official MCP SDK."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from eventide.mcp.client import MCPManager, MCPServerConfig, normalize_mcp_name


async def test_real_stdio_mcp_discovers_and_calls_tools():
    manager = MCPManager()
    server = Path(__file__).parents[1] / "examples" / "mcp_echo_server.py"
    try:
        await manager.connect(
            MCPServerConfig(
                name="demo", transport="stdio", command=sys.executable, args=(str(server),)
            )
        )
        assert "mcp__demo__eventide_demo_echo" in {tool["name"] for tool in manager.tools}
        assert "mcp__demo__eventide_demo_echo" in manager.readonly_tools
        result = await manager.handlers["mcp__demo__eventide_demo_echo"](text="transport-ok")
        assert "transport-ok" in result
    finally:
        await manager.close()


async def test_connect_all_isolates_unavailable_servers(monkeypatch):
    manager = MCPManager()
    connected = []

    async def connect(config, *, timeout=None):
        if config.name == "broken":
            raise TimeoutError("offline")
        connected.append((config.name, timeout))

    monkeypatch.setattr(manager, "connect", connect)
    failures = await manager.connect_all(
        [
            MCPServerConfig(name="broken", transport="stdio", command="bad"),
            MCPServerConfig(name="healthy", transport="stdio", command="ok"),
        ],
        timeout=0.1,
    )
    assert connected == [("healthy", 0.1)]
    assert failures == [{"server": "broken", "error": "TimeoutError: offline"}]


def test_mcp_config_parsing_and_validation(isolated_workspace):
    config = isolated_workspace / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": {
                    "local demo": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["server.py"],
                        "env": ["SAFE_VAR"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    loaded = MCPManager.load_configs(config)
    assert loaded[0].name == "local demo" and loaded[0].env == ("SAFE_VAR",)
    assert normalize_mcp_name("local demo/tool") == "local_demo_tool"
    assert MCPManager.load_configs(isolated_workspace / "missing.json") == []
    with pytest.raises(ValueError, match="unsupported"):
        MCPServerConfig.from_dict("bad", {"transport": "websocket"})
    with pytest.raises(ValueError, match="requires command"):
        MCPServerConfig.from_dict("bad", {"transport": "stdio"})
    with pytest.raises(ValueError, match="requires url"):
        MCPServerConfig.from_dict("bad", {"transport": "streamable-http"})


async def test_real_streamable_http_discovers_and_calls_tools():
    server = Path(__file__).parents[1] / "examples" / "mcp_echo_server.py"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            str(server),
            "--transport",
            "streamable-http",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    manager = MCPManager()
    try:
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if process.poll() is not None:
                raise AssertionError(process.stderr.read())
            time.sleep(0.02)
        else:
            raise AssertionError("MCP HTTP server did not start")
        await manager.connect(
            MCPServerConfig(
                name="http-demo",
                transport="streamable-http",
                url=f"http://127.0.0.1:{port}/mcp",
            )
        )
        handler = manager.handlers["mcp__http-demo__eventide_demo_sum"]
        assert "7" in await handler(a=3, b=4)
    finally:
        await manager.close()
        process.terminate()
        process.wait(timeout=5)
