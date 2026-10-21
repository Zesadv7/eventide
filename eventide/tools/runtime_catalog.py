"""Production workspace tools, without importing legacy process-global registries."""

from typing import Any

from eventide.tool_contracts import builtin_schema
from eventide.tools.filesystem import run_edit, run_glob, run_read, run_search_text, run_write


def _tool(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": builtin_schema(name),
    }


TOOLS = [
    _tool("bash", "Run a command on the local host (not a sandbox)."),
    _tool(
        "read_file",
        "Read text with one-based source line numbers (display only, not file content). "
        "Offset is zero-based. Long files report total lines and next offset.",
    ),
    _tool(
        "write_file",
        "Write a workspace file.",
    ),
    _tool(
        "edit_file",
        "Replace text once in a workspace file.",
    ),
    _tool("glob", "Find workspace paths."),
    _tool(
        "search_text", "Find literal text in UTF-8 workspace files without an index. "
        "Returns path:source-line and bounded snippets. Pattern filters paths; offset counts "
        "matching lines. Hidden paths require an explicit dot pattern. "
        "Narrow pattern on scan limit.",
    ),
    _tool("compact", "Request context compaction of completed turns."),
]


def _command_boundary(**_arguments: object) -> str:
    """Visibility marker; ToolExecutor dispatches commands to its injected executor."""
    raise RuntimeError("Commands must be executed through ToolExecutor")


HANDLERS = {
    "bash": _command_boundary,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "search_text": run_search_text,
    "compact": lambda focus="": "Compaction requested",
}
