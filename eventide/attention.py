"""Read-only attention center projections over committed Runtime facts.

Every item is derived from events and bindings already in the store; nothing
here calls a model or issues new authorization. Approval identities and raw
arguments pass through verbatim, so a display conversion can never widen what
the original approval id authorizes.
"""

from __future__ import annotations

import time
from typing import Any

from eventide.delivery_control import WebDeliveryController
from eventide.host import RuntimeHost

READ_ONLY_TOOLS = {"read_file", "glob", "search_text", "search_knowledge"}
WRITE_TOOLS = {"write_file", "edit_file", "bash"}

EXECUTION_LABELS = {
    "source": "源工作区（未隔离）",
    "worktree": "隔离工作副本",
    "snapshot": "隔离只读快照",
    "sandbox": "容器沙箱",
    "delivery": "交付 Writer 副本",
    "evaluation": "评测执行",
    "index": "索引构建",
    "worktree_control": "工作副本管理",
    "history_import": "历史导入",
    "python_experiment": "Python 实验",
}

DEFAULT_LIMIT = 50
RUN_LIMIT = 20

UNSUPPORTED_KINDS: dict[str, str] = {}


def _text(arguments: dict[str, Any], *keys: str) -> list[str]:
    return [value for key in keys if isinstance(value := arguments.get(key), str) and value]


def summarize_tool_call(
    tool: Any, arguments: Any, execution: dict[str, Any] | None
) -> dict[str, Any]:
    """Derive readable scope strictly from frozen arguments and the execution binding.

    Anything the facts do not declare stays None/未声明: network access and
    privilege elevation are never inferred from a command string.
    """
    safe_arguments = arguments if isinstance(arguments, dict) else {}
    binding = execution if isinstance(execution, dict) else {}
    kind = binding.get("kind") if isinstance(binding.get("kind"), str) else None
    working_directory = binding.get("working_directory")
    targets: list[str] = []
    command: str | None = None
    query: str | None = None
    server: str | None = None
    remote_tool: str | None = None

    if isinstance(tool, str):
        if tool == "bash":
            command = safe_arguments.get("command") if isinstance(
                safe_arguments.get("command"), str
            ) else None
        if tool in {"read_file", "write_file", "edit_file"}:
            targets.extend(_text(safe_arguments, "path"))
        if tool == "glob":
            targets.extend(_text(safe_arguments, "pattern"))
        if tool == "search_text":
            targets.extend(_text(safe_arguments, "path"))
            query = safe_arguments.get("query") if isinstance(
                safe_arguments.get("query"), str
            ) else None
        if tool == "search_knowledge":
            query = safe_arguments.get("query") if isinstance(
                safe_arguments.get("query"), str
            ) else None
        if tool.startswith("mcp__") and tool.count("__") >= 2:
            parts = tool.split("__")
            server, remote_tool = parts[1], parts[-1]

    if tool in READ_ONLY_TOOLS:
        impact = "只读"
    elif tool in WRITE_TOOLS:
        if kind == "sandbox":
            impact = "写入容器沙箱"
        elif kind in {"worktree", "snapshot", "delivery"}:
            impact = "写入隔离执行副本"
        elif kind == "source":
            impact = "写入源工作区（未隔离）"
        else:
            impact = None
    else:
        impact = None

    return {
        "tool": tool if isinstance(tool, str) else None,
        "command": command,
        "targets": targets,
        "query": query,
        "cwd": working_directory if isinstance(working_directory, str) else None,
        "execution_kind": kind,
        "execution_label": EXECUTION_LABELS.get(kind) if kind else None,
        "worktree_name": binding.get("name") if isinstance(binding.get("name"), str) else None,
        "sandbox_id": binding.get("sandbox_id") if isinstance(
            binding.get("sandbox_id"), str
        ) else None,
        "impact": impact,
        "mcp_server": server,
        "mcp_tool": remote_tool,
        "network_access": None,
        "privilege_elevation": None,
    }


def _approval_items(
    runtime: RuntimeHost, limit: int, project_id: str | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for fact in runtime.store.pending_approval_facts(limit, project_id):
        payload = fact["payload"] if isinstance(fact["payload"], dict) else {}
        run = runtime.store.get_run_summary(fact["run_id"])
        if run is None:
            continue
        approval_id = payload.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            continue
        execution = runtime.run_execution(fact["run_id"])
        run_status = run.get("status")
        items.append({
            "kind": "approval",
            "dedup_key": f"approval:{approval_id}",
            "approval_id": approval_id,
            "run_id": fact["run_id"],
            "session_id": fact["session_id"],
            "workspace_id": fact["workspace_id"],
            "ts": fact["ts"],
            "tool": payload.get("tool"),
            "arguments": payload.get("arguments"),
            "reason": payload.get("reason") if isinstance(payload.get("reason"), str) else None,
            "summary": summarize_tool_call(
                payload.get("tool"), payload.get("arguments"), execution,
            ),
            "run_status": run_status,
            "actionable": run_status in {"running", "waiting_for_user"},
            "action": {
                "method": "POST",
                "url": f"/api/runs/{fact['run_id']}/approvals/{approval_id}",
            },
            "links": {
                "run": f"/runs/{fact['run_id']}/diagnostics",
                "session": f"/chat/{fact['session_id']}",
            },
        })
    return items


def _question_items(
    runtime: RuntimeHost, limit: int, project_id: str | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for fact in runtime.store.pending_question_facts(limit, project_id):
        payload = fact["payload"] if isinstance(fact["payload"], dict) else {}
        run = runtime.store.get_run_summary(fact["run_id"])
        if run is None:
            continue
        question_id = payload.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            continue
        run_status = run.get("status")
        items.append({
            "kind": "question",
            "dedup_key": f"question:{question_id}",
            "question_id": question_id,
            "run_id": fact["run_id"],
            "session_id": fact["session_id"],
            "workspace_id": fact["workspace_id"],
            "ts": fact["ts"],
            "revision": payload.get("revision"),
            "questions": payload.get("questions"),
            "run_status": run_status,
            "actionable": (run_status in {"running", "waiting_for_user"}
                           and runtime.has_question_waiter(question_id)),
            "action": {
                "method": "POST",
                "url": f"/api/runs/{fact['run_id']}/questions/{question_id}/answers",
            },
            "links": {"session": f"/chat/{fact['session_id']}"},
        })
    return items


def _run_items(runtime: RuntimeHost, limit: int, project_id: str | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for run_id in runtime.store.recent_failed_run_ids(limit, project_id):
        run = runtime.store.get_run_summary(run_id)
        if run is None:
            continue
        session = runtime.store.get_session(run["session_id"])
        workspace_id = session["workspace_id"] if session else None
        items.append({
            "kind": "run",
            "dedup_key": f"run:{run_id}:{run.get('status')}",
            "run_id": run_id,
            "session_id": run["session_id"],
            "workspace_id": workspace_id,
            "ts": run.get("last_activity_at") or run.get("started_at"),
            "status": run.get("status"),
            "error": run.get("error"),
            "reason": run.get("reason"),
            "pending_approvals": len(run.get("pending_approvals") or {}),
            "worker_role": run.get("worker_role"),
            "parent_run_id": run.get("parent_run_id"),
            "links": {
                "run": f"/runs/{run_id}/diagnostics",
                "session": f"/chat/{run['session_id']}",
            },
        })
    return items


def _delivery_items(runtime: RuntimeHost, project_id: str | None) -> list[dict[str, Any]]:
    projects = (
        [project_id] if project_id
            else [project.project_id for project in runtime.store.list_projects()]

    )
    controller = WebDeliveryController(runtime)
    items: list[dict[str, Any]] = []
    for scope in projects:
        details = controller.list(scope)
        for detail in details:
            if detail.get("repair_status") != "passed" or detail.get("applied"):
                continue
            verification = detail.get("verification") or {}
            delivery_id = detail.get("delivery_id")
            if not isinstance(delivery_id, str) or not delivery_id:
                continue
            items.append({
                "kind": "delivery_candidate",
                "dedup_key": f"delivery:{delivery_id}",
                "delivery_id": delivery_id,
                "name": detail.get("name"),
                "workspace_id": detail.get("workspace_id"),
                "project_id": scope,
                "patch_sha256": verification.get("patch_sha256"),
                "links": {"page": "/deliveries"},
            })
    return items


def collect_attention(
    runtime: RuntimeHost,
    *,
    project_id: str | None = None,
    approval_limit: int = DEFAULT_LIMIT,
    run_limit: int = RUN_LIMIT,
) -> dict[str, Any]:
    """Project the pending-attention lists; a pure read over committed facts."""
    if project_id is not None:
        runtime.store.get_project(project_id)
    approvals = _approval_items(runtime, approval_limit, project_id)
    runs = _run_items(runtime, run_limit, project_id)
    deliveries = _delivery_items(runtime, project_id)
    questions = _question_items(runtime, approval_limit, project_id)
    return {
        "generated_at": time.time(),
        "project_id": project_id,
        "limits": {"approvals": approval_limit, "runs": run_limit, "questions": approval_limit},
        "counts": {
            "approvals": len(approvals),
            "runs": len(runs),
            "delivery_candidates": len(deliveries),
            "questions": len(questions),
        },
        "approvals": approvals,
        "runs": runs,
        "delivery_candidates": deliveries,
        "questions": questions,
        "unsupported": UNSUPPORTED_KINDS,
    }
