"""Pure projections over committed semantic RuntimeEvents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

TERMINALS = {"run.completed", "run.failed", "run.interrupted"}


class MessagesProjection:
    @staticmethod
    def project(
        events: list[dict[str, Any]],
        *,
        tool_result_content: Callable[[dict[str, Any]], str] | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        pending: set[str] = set()
        results: list[dict[str, Any]] = []
        for event in events:
            if event["partial"]:
                continue
            payload = event["payload"]
            if event["type"] in {"message.user", "message.imported", "model.response"}:
                message = payload.get("message")
                if message:
                    messages.append(message)
                    content = message.get("content")
                    if message["role"] == "assistant" and isinstance(content, list):
                        pending = {b["id"] for b in content if b.get("type") == "tool_use"}
            elif event["type"] in {"tool.completed", "tool.abandoned"}:
                call_id = payload["call_id"]
                if call_id in pending:
                    content = (
                        tool_result_content(event)
                        if tool_result_content and event["type"] == "tool.completed"
                        else payload.get("content", "Tool abandoned after interruption")
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": content,
                            "is_error": payload.get("is_error", True),
                        }
                    )
                    pending.remove(call_id)
                    if not pending:
                        messages.append({"role": "user", "content": results})
                        results = []
        if pending:
            raise ValueError("Incomplete tool history; Continue must resolve pending calls")
        return messages


class RuntimeStateProjection:
    @staticmethod
    def project(events: list[dict[str, Any]]) -> dict[str, Any]:
        state: dict[str, Any] = {
            "status": "running",
            "output": "",
            "steps": 0,
            "tool_calls": 0,
            "duration_ms": 0,
            "usage": {},
            "error": None,
            "pending_approvals": {},
            "tools": {},
            "checkpoint": None,
            "reason": None,
        }
        terminal = False
        for event in events:
            if event["partial"]:
                continue
            kind, payload = event["type"], event["payload"]
            if kind == "model.response":
                state["steps"] = payload.get("step", state["steps"])
            if kind in {"model.response", "context.compacted"}:
                for key, value in payload.get("usage", {}).items():
                    state["usage"][key] = state["usage"].get(key, 0) + value
            if kind == "workspace.checkpoint":
                state["checkpoint"] = payload.get("checkpoint")
            if kind == "tool.prepared":
                state["tool_calls"] += 1
                state["tools"][payload["call_id"]] = {**payload, "status": "prepared"}
            if kind in {"tool.completed", "tool.abandoned"}:
                state["tools"][payload["call_id"]] = {**payload, "status": kind.split(".")[1]}
            if kind == "approval.required":
                state["pending_approvals"][payload["approval_id"]] = payload
                if not terminal:
                    state["status"] = "waiting_for_user"
            if kind == "approval.resolved":
                state["pending_approvals"].pop(payload["approval_id"], None)
                if not terminal:
                    state["status"] = "running"
            if kind in TERMINALS and not terminal:
                terminal = True
                state.update({k: v for k, v in payload.items() if k in state})
                state["status"] = kind.split(".")[1]
                state["completed_at"] = event["ts"]
        return state


class TaskPlanProjection:
    """Fold plan snapshots into the session's tasks, their claims, and their evidence.

    The plan event stays the only writer: nothing here is persisted, and a task id that
    never appeared in a plan simply does not exist. `summary` is the model's claim about
    what it did; `evidence` is what the Runtime actually observed between the task first
    appearing and it being settled. Neither is proof that the work was verified, and a
    failed call is still evidence.
    """

    MAX_EVIDENCE = 8
    # The plan tool is the scheduler's own bookkeeping, not work a task can point at.
    BOOKKEEPING_TOOLS = frozenset({"todo_write"})

    @staticmethod
    def project(events: list[dict[str, Any]]) -> dict[str, Any]:
        tasks: dict[str, dict[str, Any]] = {}
        outcomes: list[tuple[int, dict[str, Any]]] = []
        active: str | None = None
        updated_seq = 0
        for event in events:
            if event["partial"]:
                continue
            kind = event["type"]
            payload = event["payload"]
            if kind == "task.plan_updated":
                updated_seq = event["session_seq"]
                active = None
                claimed: set[str] = set()
                for index, todo in enumerate(payload.get("todos", []), 1):
                    task_id = todo.get("id")
                    if not isinstance(task_id, str) or not task_id:
                        # Plans written before task identity existed carry no id.
                        continue
                    claimed.add(task_id)
                    record = tasks.setdefault(
                        task_id,
                        {
                            "id": task_id,
                            "first_seq": event["session_seq"],
                            "settled_seq": None,
                        },
                    )
                    record["order"] = index
                    record["content"] = todo.get("content", "")
                    record["status"] = todo.get("status", "pending")
                    record["summary"] = todo.get("summary")
                    if todo.get("status") in {"completed", "blocked"}:
                        if record["settled_seq"] is None:
                            record["settled_seq"] = event["session_seq"]
                    else:
                        record["settled_seq"] = None
                    if todo.get("status") == "in_progress":
                        active = task_id
                for task_id, record in tasks.items():
                    if task_id not in claimed:
                        # Dropped from the plan: keep state, drop the position.
                        record["order"] = None
            elif (
                kind in {"tool.completed", "tool.abandoned"}
                and payload.get("name") not in TaskPlanProjection.BOOKKEEPING_TOOLS
            ):
                outcomes.append(
                    (
                        event["session_seq"],
                        {
                            "run_id": event.get("run_id"),
                            "call_id": payload.get("call_id"),
                            "name": payload.get("name"),
                            "is_error": bool(
                                payload.get("is_error", kind == "tool.abandoned")
                            ),
                            "event_id": event.get("event_id"),
                            "session_seq": event["session_seq"],
                        },
                    )
                )
        for record in tasks.values():
            window = [
                entry
                for seq, entry in outcomes
                if seq > record["first_seq"]
                and (record["settled_seq"] is None or seq <= record["settled_seq"])
            ]
            record["evidence_omitted"] = max(0, len(window) - TaskPlanProjection.MAX_EVIDENCE)
            record["evidence"] = window[-TaskPlanProjection.MAX_EVIDENCE :]
        ordered = sorted(
            tasks.values(),
            key=lambda item: (item["order"] is None, item["order"] or 0, item["first_seq"]),
        )
        return {
            "tasks": [dict(item) for item in ordered],
            "active_task_id": active,
            "updated_seq": updated_seq,
        }
