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
        "Offset is zero-based. The header carries the file sha256; pass it to edit_file as "
        "expected_sha256 to fail when the file changed since this read. Long files return a "
        "continuation token bound to this working copy and content version; pass it back as "
        "continuation for the next page, which is rejected if the file changed. Very long "
        "lines are split as line:char segments with exact source coordinates.",
    ),
    _tool(
        "write_file",
        "Write a workspace file.",
    ),
    _tool(
        "edit_file",
        "Replace the single unique occurrence of old_text. Empty, missing or ambiguous "
        "matches change nothing and report bounded match locations. Pass expected_sha256 "
        "from read_file to reject the edit when the file changed since that read.",
    ),
    _tool("glob", "Find workspace paths."),
    _tool(
        "search_text", "Search UTF-8 workspace files without an index. mode=literal always "
        "works; mode=regex needs the local ripgrep backend and errors when it is missing, "
        "never degrading regex to literal. Hits render as path:line, context lines as "
        "path:line-, and sources carry the sha256 of the exact searched bytes, accepted by "
        "edit_file expected_sha256; the header reports scan/skip/timeout status, so an empty "
        "result covers only the files searched. Pattern filters paths; offset counts matching "
        "lines; hidden paths need an explicit dot.",
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
