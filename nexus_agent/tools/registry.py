"""Tool registry: schemas and handler dispatch."""

from typing import Callable


BUILTIN_TOOLS: list[dict] = []
BUILTIN_HANDLERS: dict[str, Callable] = {}


def call_tool_handler(handler: Callable | None, args: dict | None,
                      name: str) -> str:
    """Call a tool handler with uniform error handling."""
    if handler is None:
        return f"Unknown tool: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as exc:
        return f"Error: {exc}"
