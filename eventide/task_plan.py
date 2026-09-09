"""Bounded, Event Log-backed task plans for the production Runtime."""

from __future__ import annotations

from collections import Counter
from typing import Any

MAX_TODOS = 20
MAX_CONTENT_CHARS = 200
STATUSES = frozenset({"pending", "in_progress", "completed", "blocked"})

TOOL = {
    "name": "todo_write",
    "description": "Replace the current session task plan for multi-step work.",
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "maxItems": MAX_TODOS,
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "maxLength": MAX_CONTENT_CHARS},
                        "status": {"type": "string", "enum": sorted(STATUSES)},
                    },
                    "required": ["content", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["todos"],
        "additionalProperties": False,
    },
}

GUIDANCE = (
    "For work with three or more meaningful steps, call todo_write before execution. "
    "Pass the complete current plan, keep at most one item in_progress, and update it "
    "after completing or blocking a step. Skip plans for trivial requests."
)


def normalize_todos(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("todos must be an array")
    if len(value) > MAX_TODOS:
        raise ValueError(f"todos cannot contain more than {MAX_TODOS} items")
    todos: list[dict[str, str]] = []
    seen: set[str] = set()
    active = 0
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) != {"content", "status"}:
            raise ValueError(f"todo {index} must contain only content and status")
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"todo {index} content must be non-empty text")
        content = content.strip()
        if len(content) > MAX_CONTENT_CHARS:
            raise ValueError(
                f"todo {index} content cannot exceed {MAX_CONTENT_CHARS} characters"
            )
        if content in seen:
            raise ValueError(f"todo {index} duplicates another item")
        if status not in STATUSES:
            raise ValueError(f"todo {index} has unsupported status")
        active += status == "in_progress"
        seen.add(content)
        todos.append({"content": content, "status": str(status)})
    if active > 1:
        raise ValueError("only one todo may be in_progress")
    return todos


def summary(todos: list[dict[str, str]]) -> str:
    counts = Counter(todo["status"] for todo in todos)
    return (
        f"Task plan updated: {len(todos)} items; {counts['completed']} completed, "
        f"{counts['in_progress']} in progress, {counts['pending']} pending, "
        f"{counts['blocked']} blocked."
    )


def prompt(todos: list[dict[str, Any]] | None) -> str:
    """Render only unfinished work; completed details remain in the Event Log."""
    if todos is None:
        return ""
    completed = sum(todo.get("status") == "completed" for todo in todos)
    remaining = [todo for todo in todos if todo.get("status") != "completed"]
    if not remaining:
        return f"Current task plan: all {completed} items completed."
    lines = [f"Current task plan ({completed} completed):"]
    lines.extend(
        f"{index}. [{todo.get('status', 'pending')}] {todo.get('content', '')}"
        for index, todo in enumerate(remaining, 1)
    )
    return "\n".join(lines)
