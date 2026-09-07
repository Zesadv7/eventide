"""Low-level tool dispatch helper, separated to avoid circular imports."""

from collections.abc import Callable


def call_tool_handler(handler: Callable | None, args: dict | None, name: str) -> str:
    """Call a tool handler with uniform error handling."""
    if handler is None:
        return f"Unknown tool: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as exc:
        return f"Error: {exc}"
