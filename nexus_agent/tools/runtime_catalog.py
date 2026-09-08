"""Production workspace tools, without importing legacy process-global registries."""

from typing import Any

from nexus_agent.tools.bash import run_bash
from nexus_agent.tools.filesystem import run_edit, run_glob, run_read, run_write


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


TEXT = {"type": "string"}
TOOLS = [
    _tool(
        "bash", "Run a command on the local host (not a sandbox).", {"command": TEXT}, ["command"]
    ),
    _tool(
        "read_file",
        "Read a workspace text file.",
        {"path": TEXT, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        ["path"],
    ),
    _tool(
        "write_file",
        "Write a workspace file.",
        {"path": TEXT, "content": TEXT},
        ["path", "content"],
    ),
    _tool(
        "edit_file",
        "Replace text once in a workspace file.",
        {"path": TEXT, "old_text": TEXT, "new_text": TEXT},
        ["path", "old_text", "new_text"],
    ),
    _tool("glob", "Find workspace paths.", {"pattern": TEXT}, ["pattern"]),
    _tool("compact", "Request context compaction of completed turns.", {"focus": TEXT}, []),
]
HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "compact": lambda focus="": "Compaction requested",
}
