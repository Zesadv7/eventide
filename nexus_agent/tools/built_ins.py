"""Built-in tools that don't fit filesystem/bash: todos, compact, skills, subagent."""

import ast
import json

from nexus_agent.memory.skills import load_skill
from nexus_agent.teams.subagent import spawn_subagent

CURRENT_TODOS: list[dict] = []


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    """Update the in-memory todo list for the current session."""
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    return f"Updated {len(CURRENT_TODOS)} todos"


def run_compact(focus: str = "") -> str:
    """Summarize earlier conversation and continue with compacted context."""
    # `focus` is accepted for compatibility but compaction is global.
    _ = focus
    return "Compaction requested. The loop will replace history with a summary."


def run_task(description: str) -> str:
    """Launch a focused subagent and return its final summary."""
    return spawn_subagent(description)


def run_load_skill(name: str) -> str:
    """Load the full content of a skill by name."""
    return load_skill(name)
