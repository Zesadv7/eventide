"""Per-run policy helpers for composer-selected run modes.

Pure functions only: wiring into the run loop happens in ``host.py``. ``plan``
restricts the visible tool catalog to read-only tools; ``agent`` auto-approves
ASK decisions while every decision still lands in the event log as audit.
"""

from __future__ import annotations

RUN_MODES = ("auto", "plan", "agent")

# `plan` keeps research and plan bookkeeping; anything with side effects
# (bash, write/edit, MCP) disappears from the catalog so the model cannot call it.
PLAN_READONLY_TOOLS = frozenset(
    {"read_file", "glob", "compact", "read_tool_result", "todo_write", "load_skill"}
)

_PLAN_NOTE = (
    "Read-only planning mode: only read and search tools are available. "
    "Do not attempt to modify the workspace, run side-effecting commands, or "
    "call external tools; produce findings and a plan instead."
)


def normalize_mode(value: object) -> str:
    """Unknown or missing modes fall back to ``auto`` (contract: never reject)."""
    return value if value in RUN_MODES else "auto"


def filter_tool_catalog(tools: list[dict], mode: str) -> list[dict]:
    """Return the tool catalog visible to this run under ``mode``."""
    if mode != "plan":
        return list(tools)
    return [tool for tool in tools if tool.get("name") in PLAN_READONLY_TOOLS]


def mode_system_note(mode: str) -> str:
    """Extra system-prompt sentence for the mode; empty for auto/agent."""
    if mode != "plan":
        return ""
    return _PLAN_NOTE


def should_auto_approve(mode: str) -> bool:
    """Agent mode approves ASK decisions without waiting for the user."""
    return mode == "agent"
