"""Production workspace tools, without importing legacy process-global registries."""

from typing import Any

from eventide.tool_contracts import builtin_schema
from eventide.tools.filesystem import run_edit, run_glob, run_read, run_write


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
        "Read a workspace text file. Long files are paged; the header reports the "
        "total line count and the next offset.",
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
    "compact": lambda focus="": "Compaction requested",
}
