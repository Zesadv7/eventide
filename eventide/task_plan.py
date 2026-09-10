"""Bounded, Event Log-backed task plans for the production Runtime."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

MAX_TODOS = 20
MAX_CONTENT_CHARS = 200
STATUSES = frozenset({"pending", "in_progress", "completed", "blocked"})
ID_PATTERN = re.compile(r"t[1-9][0-9]{0,3}")
ITEM_KEYS = frozenset({"id", "content", "status"})

TOOL = {
    "name": "todo_write",
    "description": (
        "Replace the session task plan. Use it for three or more meaningful steps: call it "
        "before execution, then again after each completion, block, or switch. Send the "
        "complete plan, keep at most one item in_progress, and echo back the id of every item "
        "you keep so its identity survives rewording. Skip plans for trivial requests."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "maxItems": MAX_TODOS,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "maxLength": 16},
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


def _claimed_sequence(previous: list[dict[str, Any]] | None) -> int:
    """Highest task sequence already handed out, so ids are never recycled."""
    highest = 0
    for item in previous or []:
        task_id = item.get("id")
        if isinstance(task_id, str) and ID_PATTERN.fullmatch(task_id):
            highest = max(highest, int(task_id[1:]))
    return highest


def plan_update(
    value: object,
    previous: list[dict[str, Any]] | None = None,
    next_task_seq: int | None = None,
) -> tuple[list[dict[str, str]], int]:
    """Validate a full plan and give every item a stable identity.

    Identity survives rewording: an item keeps its id when the model echoes it back, or
    when an unclaimed previous item still carries exactly the same content. Ids are handed
    out monotonically and never recycled, so a completed reference cannot silently point at
    a different task later.
    """
    if not isinstance(value, list):
        raise ValueError("todos must be an array")
    if len(value) > MAX_TODOS:
        raise ValueError(f"todos cannot contain more than {MAX_TODOS} items")
    available = {str(item.get("id")): item for item in previous or [] if item.get("id")}
    sequence = max(
        next_task_seq if isinstance(next_task_seq, int) else 0,
        _claimed_sequence(previous) + 1,
    )
    todos: list[dict[str, str]] = []
    seen_content: set[str] = set()
    claimed: set[str] = set()
    active = 0
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or not set(item) <= ITEM_KEYS:
            raise ValueError(f"todo {index} must contain only id, content and status")
        if "content" not in item or "status" not in item:
            raise ValueError(f"todo {index} must contain content and status")
        content = item.get("content")
        status = item.get("status")
        task_id = item.get("id")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"todo {index} content must be non-empty text")
        content = content.strip()
        if len(content) > MAX_CONTENT_CHARS:
            raise ValueError(
                f"todo {index} content cannot exceed {MAX_CONTENT_CHARS} characters"
            )
        if content in seen_content:
            raise ValueError(f"todo {index} duplicates another item")
        if status not in STATUSES:
            raise ValueError(f"todo {index} has unsupported status")
        if task_id is None:
            # Reuse an unclaimed previous identity when the plan was rewritten verbatim.
            task_id = next(
                (
                    known
                    for known, known_item in available.items()
                    if known not in claimed
                    and str(known_item.get("content", "")).strip() == content
                ),
                None,
            )
        else:
            if not isinstance(task_id, str) or not ID_PATTERN.fullmatch(task_id):
                raise ValueError(f"todo {index} has an invalid task id")
            if task_id not in available:
                raise ValueError(f"todo {index} references unknown task id {task_id}")
        if task_id is None:
            task_id = f"t{sequence}"
            sequence += 1
        elif task_id in claimed:
            raise ValueError(f"todo {index} reuses task id {task_id}")
        claimed.add(task_id)
        seen_content.add(content)
        active += status == "in_progress"
        todos.append({"id": task_id, "content": content, "status": str(status)})
    if active > 1:
        raise ValueError("only one todo may be in_progress")
    return todos, sequence


def normalize_todos(value: object) -> list[dict[str, str]]:
    """Validate a standalone plan without any previous identity to preserve."""
    return plan_update(value)[0]


def summary(todos: list[dict[str, str]]) -> str:
    counts = Counter(todo["status"] for todo in todos)
    return (
        f"Task plan updated: {len(todos)} items; {counts['completed']} completed, "
        f"{counts['in_progress']} in progress, {counts['pending']} pending, "
        f"{counts['blocked']} blocked."
    )


def active_id(todos: list[dict[str, Any]] | None) -> str | None:
    """The one task the plan currently marks as being worked on."""
    for todo in todos or []:
        if todo.get("status") == "in_progress":
            task_id = todo.get("id")
            return task_id if isinstance(task_id, str) and task_id else None
    return None


def prompt(todos: list[dict[str, Any]] | None) -> str:
    """Render only unfinished work; completed details remain in the Event Log."""
    if todos is None:
        return ""
    completed = sum(todo.get("status") == "completed" for todo in todos)
    remaining = [todo for todo in todos if todo.get("status") != "completed"]
    if not remaining:
        return f"Current task plan: all {completed} items completed."
    lines = [f"Current task plan ({completed} completed):"]
    for index, todo in enumerate(remaining, 1):
        task_id = todo.get("id")
        label = f"{task_id}: " if task_id else ""
        lines.append(
            f"{index}. [{todo.get('status', 'pending')}] {label}{todo.get('content', '')}"
        )
    if active_id(todos) is None:
        lines.append("No task is in_progress; mark the one you are working on.")
    return "\n".join(lines)
