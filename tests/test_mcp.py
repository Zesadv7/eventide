"""Tests for MCP client and tool pool assembly."""

from eventide.mcp.client import assemble_tool_pool, connect_mcp, mcp_clients


def test_connect_mcp_docs():
    msg = connect_mcp("docs")
    assert "Connected" in msg
    assert "docs" in mcp_clients


def test_assemble_tool_pool_grows():
    connect_mcp("docs")
    tools, handlers = assemble_tool_pool()
    names = [t["name"] for t in tools]
    assert "mcp__docs__search" in names
    assert "mcp__docs__search" in handlers


def test_mcp_handler_calls_tool():
    connect_mcp("docs")
    tools, handlers = assemble_tool_pool()
    result = handlers["mcp__docs__search"](query="agent loop")
    assert "Found 3 results" in result
