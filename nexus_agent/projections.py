"""Pure projections over committed semantic RuntimeEvents."""

from __future__ import annotations

from typing import Any

TERMINALS = {"run.completed", "run.failed", "run.interrupted"}


class MessagesProjection:
    @staticmethod
    def project(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": payload.get("content", "Tool abandoned after interruption"),
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
