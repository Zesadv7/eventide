"""Production workspace tools, without importing legacy process-global registries."""

from typing import Any

from eventide.tool_contracts import builtin_schema
from eventide.tools.filesystem import (
    read_file_output,
    run_edit,
    run_glob,
    run_write,
    search_text_output,
)


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
        "Read text with one-based display-only lines; offset is zero-based. "
        "Header sha256 binds edit_file.expected_sha256. Pass continuation to page; "
        "tokens bind working copy and file version, rejecting changes. "
        "Long lines use exact line:char segments.",
    ),
    _tool(
        "write_file",
        "Write a workspace file.",
    ),
    _tool(
        "edit_file",
        "Replace one unique old_text match; empty/missing/ambiguous matches never write "
        "and report bounded locations. expected_sha256 from read_file rejects stale reads.",
    ),
    _tool("glob", "Find workspace paths."),
    _tool(
        "search_text", "Search UTF-8 files without an index. literal always works; "
        "regex requires ripgrep and never falls back. Hits: path:line; context: path:line-. "
        "Source sha256 binds edit_file. Header reports scan/skip/timeout; empty means only "
        "scanned files. pattern filters paths; offset counts matching lines; "
        "hidden paths need an explicit dot.",
    ),
    _tool("compact", "Request context compaction of completed turns."),
]


def _command_boundary(**_arguments: object) -> str:
    """Visibility marker; ToolExecutor dispatches commands to its injected executor."""
    raise RuntimeError("Commands must be executed through ToolExecutor")


HANDLERS = {
    "bash": _command_boundary,
    "read_file": read_file_output,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "search_text": search_text_output,
    "compact": lambda focus="": "Compaction requested",
}
