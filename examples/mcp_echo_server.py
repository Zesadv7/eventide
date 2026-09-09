"""Small real MCP server for Eventide integration demos."""

import argparse

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("eventide_demo_mcp")


@mcp.tool(
    name="eventide_demo_echo",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def eventide_demo_echo(text: str) -> str:
    """Return exactly the supplied text for transport and tool-call verification."""
    return text


@mcp.tool(
    name="eventide_demo_sum",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def eventide_demo_sum(a: float, b: float) -> float:
    """Add two numbers and return the result."""
    return a + b


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    mcp.run(transport=args.transport, host=args.host, port=args.port)
