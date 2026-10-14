"""Bounded, Event Log-backed task plans for the production Runtime."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

MAX_TODOS = 20
MAX_CONTENT_CHARS = 200
MAX_SUMMARY_CHARS = 300
MAX_PROMPT_SUMMARY_CHARS = 120
STATUSES = frozenset({"pending", "in_progress", "completed", "blocked"})
ID_PATTERN = re.compile(r"t[1-9][0-9]{0,3}")
ITEM_KEYS = frozenset({"id", "content", "status", "summary", "depends_on"})

TOOL = {
    "name": "todo_write",
    "description": (
        "Replace the full plan for three or more meaningful steps before execution and "
        "after state changes. Keep existing ids and at most one in_progress item; summarize "
        "what you did, not correctness, on completion. Optional "
        "depends_on lists prerequisite ids, which must be completed before starting or "
        "completing the item. New plans allocate t1, t2, etc. in item order. Omitted "
        "dependencies are kept; use [] to clear."
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
                        "summary": {"type": "string", "maxLength": MAX_SUMMARY_CHARS},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": MAX_TODOS,
                            "uniqueItems": True,
                        },
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
) -> tuple[list[dict[str, Any]], int]:
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
    todos: list[dict[str, Any]] = []
    seen_content: set[str] = set()
    claimed: set[str] = set()
    active = 0
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or not set(item) <= ITEM_KEYS:
            raise ValueError(
                f"todo {index} must contain only id, content, status and summary, "
                "plus optional depends_on"
            )
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
        if not isinstance(status, str) or status not in STATUSES:
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
        todos.append(_item(task_id, content, str(status), item, available, index))
    if active > 1:
        raise ValueError("only one todo may be in_progress")
    _validate_dependencies(todos)
    return todos, sequence


def _item(
    task_id: str,
    content: str,
    status: str,
    item: dict[str, Any],
    available: dict[str, dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    """Attach the completion claim and preserve or replace prerequisite references."""
    summary = item.get("summary")
    prior = available.get(task_id)
    settled = prior.get("status") if prior else None
    if status == "completed":
        if summary is None and settled == "completed":
            # The plan is a full snapshot; keep the earlier claim rather than lose it.
            summary = prior.get("summary") if prior else None
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"todo {index} must summarize what it completed")
    elif status == "blocked":
        if summary is not None and not isinstance(summary, str):
            raise ValueError(f"todo {index} summary must be text")
    elif summary is not None:
        raise ValueError(f"todo {index} may only summarize a completed or blocked item")
    if isinstance(summary, str):
        summary = summary.strip()
        if len(summary) > MAX_SUMMARY_CHARS:
            raise ValueError(
                f"todo {index} summary cannot exceed {MAX_SUMMARY_CHARS} characters"
            )
    result: dict[str, Any] = {"id": task_id, "content": content, "status": status}
    if summary:
        result["summary"] = summary
    if "depends_on" in item or (prior is not None and "depends_on" in prior):
        dependencies = item.get("depends_on", prior.get("depends_on") if prior else [])
        if not isinstance(dependencies, list):
            raise ValueError(f"todo {index} depends_on must be an array")
        if len(dependencies) > MAX_TODOS:
            raise ValueError(f"todo {index} cannot have more than {MAX_TODOS} dependencies")
        seen: set[str] = set()
        for dependency in dependencies:
            if not isinstance(dependency, str) or not ID_PATTERN.fullmatch(dependency):
                raise ValueError(f"todo {index} has an invalid dependency id")
            if dependency == task_id:
                raise ValueError(f"todo {index} cannot depend on itself")
            if dependency in seen:
                raise ValueError(f"todo {index} has duplicate dependency {dependency}")
            seen.add(dependency)
        result["depends_on"] = list(dependencies)
    return result


def _validate_dependencies(todos: list[dict[str, Any]]) -> None:
    """Check the complete candidate snapshot before any plan event can be committed."""
    by_id = {todo["id"]: todo for todo in todos}
    for todo in todos:
        for dependency in todo.get("depends_on", []):
            if dependency not in by_id:
                raise ValueError(f"task {todo['id']} references unknown dependency {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("task dependencies cannot contain a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].get("depends_on", []):
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)
    for todo in todos:
        if todo["status"] not in {"in_progress", "completed"}:
            continue
        unmet = [
            dependency
            for dependency in todo.get("depends_on", [])
            if by_id[dependency]["status"] != "completed"
        ]
        if unmet:
            raise ValueError(
                f"task {todo['id']} requires completed dependencies: {', '.join(unmet)}"
            )


def normalize_todos(value: object) -> list[dict[str, Any]]:
    """Validate a standalone plan without any previous identity to preserve."""
    return plan_update(value)[0]


def summary(todos: list[dict[str, Any]]) -> str:
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
    header = f"Current task plan ({completed} completed"
    latest = next(
        (
            todo
            for todo in reversed(todos)
            if todo.get("status") == "completed" and todo.get("summary")
        ),
        None,
    )
    if latest:
        # Keep the newest claim in view, capped so the plan block stays bounded.
        claim = str(latest["summary"])
        if len(claim) > MAX_PROMPT_SUMMARY_CHARS:
            claim = claim[:MAX_PROMPT_SUMMARY_CHARS].rstrip() + "..."
        header += f", last: {latest.get('id') or '?'} {claim}"
    lines = [header + "):"]
    for index, todo in enumerate(remaining, 1):
        task_id = todo.get("id")
        label = f"{task_id}: " if task_id else ""
        dependencies = todo.get("depends_on", [])
        suffix = f" (depends on {', '.join(dependencies)})" if dependencies else ""
        lines.append(
            f"{index}. [{todo.get('status', 'pending')}] {label}{todo.get('content', '')}{suffix}"
        )
    if active_id(todos) is None:
        lines.append("No task is in_progress; mark the one you are working on.")
    return "\n".join(lines)
