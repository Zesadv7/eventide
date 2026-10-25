"""Fact-derived, readable failure reports bound to an Event Log high-water mark.

The report is a pure projection of this Run's committed events: it never runs
models or tools, never invents causes for missing evidence, and never rewrites
an older report. ``report_version`` pins the narrative format while
``high_water`` pins the exact event cut, so JSON and Markdown downloads and a
later regeneration with the same cut describe the same facts. Text copied from
events is redacted and bounded; prompts, source code, conversation bodies and
full tool outputs are never included.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from eventide.normalization import redact

REPORT_VERSION = 1
_SOURCE = "eventide.event_log"
_TEXT_LIMIT = 300
_LIST_LIMIT = 20
_TOOL_LIMIT = 100
_ATTEMPT_LIMIT = 100

_TERMINALS = ("run.completed", "run.failed", "run.interrupted")
_REASON_LABELS = {
    "cancelled": "用户停止",
    "token_budget": "已观测 Token 预算达到上限",
    "step_budget": "步数达到上限",
    "task_step_budget": "任务步数达到上限",
    "unknown_tool_outcome": "工具结果未知",
    "host_stopped_before_dispatch": "宿主在派发前停止，Run 未开始执行",
    "budget_blocked": "预算检查阻止了模型请求",
    "repeated_tool_failure": "同一工具反复失败",
    "admission_failed": "准入失败",
}
_STATUS_LABELS = {
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
    "unfinished": "未完成（缺少终态）",
    "unknown": "未知",
}
_NOT_IN_SLICE = "当前报告版本不包含任务图/恢复中心关系与恢复动作入口。"


def collect_run_diagnostics(
    store: Any, run_id: str, high_water: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Select this Run's events up to an optional high-water cut.

    Returns the included canonical events and the effective high-water mark:
    the largest included ``session_seq`` (0 when the Run has no included
    events). A later call with the same mark reproduces the same facts even
    after new events are appended.
    """
    if high_water is not None and (
        not isinstance(high_water, int) or isinstance(high_water, bool) or high_water < 0
    ):
        raise ValueError("high_water must be a non-negative integer")
    if store.get_run_identity(run_id) is None:
        raise ValueError("Run not found")
    events = store.run_events(run_id)
    if high_water is not None:
        events = [event for event in events if event.get("session_seq", 0) <= high_water]
    bound = max((int(event["session_seq"]) for event in events), default=0)
    return events, bound


def _bounded(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else redact(value, max_text=_TEXT_LIMIT)
    text = str(text)
    text = " ".join(text.split())
    if not text:
        return None
    clipped = str(redact(text, max_text=_TEXT_LIMIT))
    return clipped


def _iso(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return None
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()


def _ref(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_type": event.get("type"),
        "at": event.get("ts"),
        "call_id": payload.get("call_id") if isinstance(payload.get("call_id"), str) else None,
        "attempt_id": (
            payload.get("attempt_id") if isinstance(payload.get("attempt_id"), str) else None
        ),
    }


def _tool_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    tools: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    truncated = False
    for event in events:
        payload = event["payload"]
        kind = event["type"]
        if kind == "tool.prepared":
            call_id = str(payload.get("call_id", ""))
            if not call_id or call_id in tools:
                continue
            if len(order) >= _TOOL_LIMIT:
                truncated = True
                continue
            order.append(call_id)
            tools[call_id] = {
                "call_id": call_id,
                "name": _bounded(payload.get("name")) or "unknown",
                "readonly": payload.get("readonly") is True,
                "status": "unresolved",
                "detail": None,
                "duration_ms": None,
                "at": event.get("ts"),
            }
        elif kind in {"tool.completed", "tool.outcome_unknown", "tool.abandoned"}:
            call_id = str(payload.get("call_id", ""))
            current = tools.get(call_id)
            if current is None:
                continue
            if kind == "tool.completed":
                current["status"] = "failed" if payload.get("is_error") else "completed"
                metadata = payload.get("metadata")
                if isinstance(metadata, Mapping):
                    duration = metadata.get("duration_ms")
                    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                        current["duration_ms"] = float(duration)
                current["detail"] = _bounded(payload.get("error"))
            elif kind == "tool.outcome_unknown":
                current["status"] = "unknown"
                current["detail"] = (
                    _bounded(payload.get("error")) or "工具结果未确认，实际副作用未知"
                )
            else:
                current["status"] = "abandoned"
                current["detail"] = "调用被放弃，未记录结果"
    listed = [tools[call_id] for call_id in order[:_TOOL_LIMIT]]
    return listed, truncated


def _attempt_facts(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, bool]:
    started: dict[str, dict[str, Any]] = {}
    finished: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event["payload"]
        attempt_id = payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            continue
        if event["type"] == "model.attempt_started":
            started.setdefault(attempt_id, event)
        elif event["type"] == "model.attempt_finished":
            if attempt_id in finished and finished[attempt_id]["payload"] != payload:
                return None, True
            finished[attempt_id] = event
    if not started and not finished:
        return [], False
    rows: list[dict[str, Any]] = []
    truncated = False

    def row(attempt_id: str, start: dict[str, Any] | None, end: dict[str, Any] | None) -> dict:
        event = start or end
        assert event is not None
        payload = event["payload"]
        usage_source = "unknown"
        if end is not None:
            source = end["payload"].get("usage_source")
            if isinstance(source, str) and source in {"reported", "estimated", "unknown"}:
                usage_source = source
        status = "unknown"
        if end is not None:
            claimed = end["payload"].get("status")
            status = claimed if claimed in {"completed", "failed", "cancelled"} else "unknown"
        return {
            "attempt_id": attempt_id,
            "attempt": payload.get("attempt")
            if isinstance(payload.get("attempt"), int)
            else None,
            "kind": _bounded(payload.get("kind")),
            "status": status,
            "usage_source": usage_source,
            "model": _bounded(payload.get("model")),
            "started_at": start.get("ts") if start else None,
            "finished_at": end.get("ts") if end else None,
        }

    for attempt_id, start in started.items():
        if len(rows) >= _ATTEMPT_LIMIT:
            truncated = True
            break
        rows.append(row(attempt_id, start, finished.get(attempt_id)))
    for attempt_id, end in finished.items():
        if attempt_id in started:
            continue
        if len(rows) >= _ATTEMPT_LIMIT:
            truncated = True
            break
        rows.append(row(attempt_id, None, end))
    return rows, truncated


def build_run_diagnostics(
    events: Iterable[Mapping[str, Any]], *, run_id: str, high_water: int
) -> dict[str, Any]:
    """Derive the bounded Chinese report from canonical events of one Run."""
    canonical: list[dict[str, Any]] = []
    for event in events:
        if (
            not isinstance(event, Mapping)
            or not isinstance(event.get("payload"), Mapping)
            or not isinstance(event.get("session_seq"), int)
        ):
            raise ValueError("Diagnostics requires canonical events with payloads and sequences")
        item = dict(event)
        item["payload"] = dict(item["payload"])
        canonical.append(item)
    canonical.sort(key=lambda event: event["session_seq"])

    known_causes: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    unavailable: list[str] = []

    def add_known(code: str, label: str, detail: str | None, event: dict[str, Any]) -> None:
        if len(known_causes) >= _LIST_LIMIT:
            return
        known_causes.append(
            {"code": code, "label": label, "detail": detail, "ref": _ref(event, event["payload"])}
        )

    def add_pending(
        code: str, label: str, detail: str | None, event: dict[str, Any]
    ) -> None:
        if len(pending) >= _LIST_LIMIT:
            return
        pending.append(
            {"code": code, "label": label, "detail": detail, "ref": _ref(event, event["payload"])}
        )

    intent: dict[str, Any] = {}
    for event in canonical:
        if event["type"] in {"run.admitted", "run.started"}:
            intent.update(event["payload"])
    terminal = next((event for event in canonical if event["type"] in _TERMINALS), None)
    terminal_payload = terminal["payload"] if terminal else {}
    reason = terminal_payload.get("reason") if terminal else None
    if terminal is not None and isinstance(reason, str) and reason in _REASON_LABELS:
        add_known("stop_reason", "停止原因", _REASON_LABELS[reason], terminal)
    if terminal is not None and terminal["type"] == "run.failed":
        add_known(
            "terminal_error", "运行失败记录的错误",
            _bounded(terminal_payload.get("error")), terminal,
        )
    elif terminal is not None and terminal["type"] == "run.interrupted":
        add_known(
            "terminal_error", "中断记录的错误",
            _bounded(terminal_payload.get("error")), terminal,
        )

    for event in canonical:
        payload = event["payload"]
        if event["type"] == "model.retry":
            add_known(
                "model_request_error",
                "模型请求失败（事件记录的错误）",
                _bounded(payload.get("error")),
                event,
            )
        elif event["type"] == "budget.blocked":
            add_known("budget_blocked", "预算阻止", _bounded(payload.get("violations")), event)

    tools, tools_truncated = _tool_events(canonical)
    attempts, attempts_truncated = _attempt_facts(canonical)
    if attempts is None:
        unavailable.append("模型尝试事实冲突，attempt 明细不可用")
        attempts = []
        attempts_truncated = True

    for event in canonical:
        payload = event["payload"]
        kind = event["type"]
        if kind == "tool.outcome_unknown":
            add_pending(
                "tool_unknown",
                "工具结果未知",
                _bounded(payload.get("error")) or "实际副作用未确认",
                event,
            )
        elif kind == "tool.completed" and payload.get("is_error"):
            add_pending(
                "tool_failed_outcome",
                "工具调用返回失败",
                _bounded(payload.get("error")),
                event,
            )
    for tool in tools:
        if tool["status"] == "unresolved":
            pending.append(
                {
                    "code": "tool_unresolved",
                    "label": "工具调用没有记录结果",
                    "detail": f"调用 {tool['call_id']}（{tool['name']}）在报告范围内没有结果事实",
                    "ref": {"event_type": "tool.prepared", "at": tool["at"],
                            "call_id": tool["call_id"], "attempt_id": None},
                }
            )
            if len(pending) >= _LIST_LIMIT:
                break
    for row in attempts:
        if row["status"] == "unknown":
            add_pending(
                "attempt_unfinished",
                "模型请求没有可确认的完成事实",
                f"attempt {row['attempt_id']} 没有完成事实或状态未知",
                {"type": "model.attempt_started", "ts": row["started_at"],
                 "payload": {"attempt_id": row["attempt_id"]}},
            )
        elif row["usage_source"] == "unknown":
            add_pending(
                "usage_unknown",
                "模型请求用量未知",
                f"attempt {row['attempt_id']} 没有可确认的用量事实",
                {"type": "model.attempt_finished", "ts": row["finished_at"],
                 "payload": {"attempt_id": row["attempt_id"]}},
            )

    facts_available = bool(canonical)
    status = "unknown"
    if not facts_available:
        unavailable.insert(0, "Run 事件记录缺失，无法还原执行过程")
        pending.append(
            {
                "code": "no_events",
                "label": "没有可用的事件记录",
                "detail": "报告无法确认该 Run 发生过什么",
                "ref": None,
            }
        )
    elif terminal is None:
        status = "unfinished"
        add_pending(
            "no_terminal",
            "缺少终态事件",
            "Run 可能仍在执行，或宿主在记录终态前停止",
            canonical[-1],
        )
    else:
        status = str(terminal["type"].removeprefix("run."))

    steps = sum(1 for event in canonical if event["type"] == "model.response")
    tool_calls = sum(1 for event in canonical if event["type"] == "tool.prepared")
    counts = {
        "steps": steps,
        "tool_calls": tool_calls,
        "tools_completed": sum(1 for tool in tools if tool["status"] == "completed"),
        "tools_failed": sum(1 for tool in tools if tool["status"] == "failed"),
        "tools_unknown": sum(1 for tool in tools if tool["status"] == "unknown"),
        "tools_abandoned": sum(1 for tool in tools if tool["status"] == "abandoned"),
        "tools_unresolved": sum(1 for tool in tools if tool["status"] == "unresolved"),
        "attempts": len(attempts or []),
        "attempts_usage_unknown": sum(
            1 for row in attempts or [] if row["usage_source"] == "unknown"
        ),
    }

    started_at = next(
        (
            event.get("ts")
            for event in canonical
            if event["type"] in {"run.admitted", "run.started"}
        ),
        None,
    )
    ended_at = terminal.get("ts") if terminal else None
    status_label = _STATUS_LABELS.get(status, status)
    scope = (
        f"模式 {intent['mode']}" if isinstance(intent.get("mode"), str) else "模式未知"
    )
    identity_bits = [scope]
    if isinstance(intent.get("provider"), str):
        identity_bits.append(f"提供方 {intent['provider']}")
    if isinstance(intent.get("model"), str):
        identity_bits.append(f"模型 {intent['model']}")
    span = ""
    start_iso, end_iso = _iso(started_at), _iso(ended_at)
    if start_iso and end_iso:
        span = f"从 {start_iso} 到 {end_iso}（UTC）。"
    elif start_iso:
        span = f"自 {start_iso}（UTC）开始，尚未记录结束。"
    if not facts_available:
        what = "没有可用的事件记录，无法生成事实摘要。"
        completed = "无可用事实。"
        where = None
        unknowns = ["事件记录缺失，无法确认执行过程。"]
        next_steps = ["没有可依据的事件事实；请先确认 Run 身份或查看历史页。"]
    else:
        what = f"Run {span}状态：{status_label}。" if span else f"Run 状态：{status_label}。"
        what += f"执行身份：{'，'.join(identity_bits)}。"
        what += f"记录到 {steps} 次模型响应、{tool_calls} 次工具调用。"
        completed = (
            f"已完成 {counts['tools_completed']} 次成功工具调用；"
            f"失败 {counts['tools_failed']} 次，结果未知 {counts['tools_unknown']} 次。"
        )
        if terminal is not None and terminal["type"] == "run.completed":
            completed = f"Run 正常完成。{completed}"
            where = None
        else:
            completed = f"停止前：{completed}"
            where = _where_stopped(canonical, terminal, terminal_payload)
        unknowns = [
            item["label"] if not item["detail"] else f"{item['label']}：{item['detail']}"
            for item in pending
        ][: _LIST_LIMIT] or ["没有记录到结果未知的项目。"]
        next_steps = _next_steps(status, reason if isinstance(reason, str) else None, pending)
    truncated = (
        tools_truncated
        or attempts_truncated
        or len(pending) >= _LIST_LIMIT
        or any(
            "chars truncated]" in text
            for text in [
                *(cause["detail"] or "" for cause in known_causes),
                *(item["detail"] or "" for item in pending),
                *(tool["detail"] or "" for tool in tools),
                *(row["kind"] or "" for row in attempts),
                *(row["model"] or "" for row in attempts),
            ]
        )
    )
    return {
        "report_version": REPORT_VERSION,
        "source": _SOURCE,
        "run_id": run_id,
        "high_water": high_water,
        "facts_available": facts_available,
        "status": status,
        "stop_reason": reason if isinstance(reason, str) else None,
        "mode": intent.get("mode") if isinstance(intent.get("mode"), str) else None,
        "provider": intent.get("provider") if isinstance(intent.get("provider"), str) else None,
        "model": intent.get("model") if isinstance(intent.get("model"), str) else None,
        "started_at": started_at,
        "ended_at": ended_at,
        "counts": counts,
        "summary": {
            "what_happened": what,
            "where_stopped": where,
            "completed": completed,
            "unknowns": unknowns,
            "next_steps": next_steps,
        },
        "known_causes": known_causes,
        "pending": pending,
        "tools": tools,
        "attempts": attempts or [],
        "unavailable": unavailable,
        "notes": [_NOT_IN_SLICE],
        "truncated": truncated,
    }


def _where_stopped(
    canonical: list[dict[str, Any]],
    terminal: dict[str, Any] | None,
    terminal_payload: Mapping[str, Any],
) -> str:
    if terminal is not None:
        reason = terminal_payload.get("reason")
        label = _REASON_LABELS.get(reason) if isinstance(reason, str) else None
        if terminal["type"] == "run.failed":
            detail = _bounded(terminal_payload.get("error"))
            return f"失败于事件 {terminal['type']}：{detail or '事件没有记录错误细节'}"
        if label:
            detail = _bounded(terminal_payload.get("error"))
            return f"停止于：{label}" + (f"（{detail}）" if detail else "")
        return f"停止于事件 {terminal['type']}。"
    last = canonical[-1]
    payload = last["payload"]
    if last["type"] == "tool.prepared":
        name = _bounded(payload.get("name")) or "unknown"
        return f"停在工具调用“{name}”之后，尚未记录到结果"
    if last["type"] in {"model.request", "model.attempt_started"}:
        return "停在模型请求之后，尚未记录到响应"
    return f"停在事件 {last['type']} 之后，尚未记录终态"


def _next_steps(
    status: str, reason: str | None, pending: list[dict[str, Any]]
) -> list[str]:
    steps: list[str] = []
    codes = {item["code"] for item in pending}
    if status == "unknown":
        return ["没有可依据的事件事实；请先确认 Run 身份或查看历史页。"]
    if "tool_unknown" in codes or "tool_unresolved" in codes:
        steps.append("先核查未知结果工具的实际副作用，再决定继续或重做")
    if status == "interrupted" and reason in {
        "cancelled", "step_budget", "task_step_budget", "token_budget",
    }:
        steps.append("可在会话中使用 Continue 继续（将开始新的 Run）")
    if status == "failed":
        steps.append("根据已知原因排查后重新发起任务")
    if status == "unfinished":
        steps.append("刷新报告确认是否已产生终态")
        steps.append("若 Run 仍在执行且需要停止，可在会话中停止该 Run")
    if status == "completed":
        steps.append("运行已成功结束，无需恢复动作")
    return steps or ["查看事件时间线与用量，确认后续处理方式。"]


def render_diagnostics_markdown(report: Mapping[str, Any]) -> str:
    """Render the Chinese Markdown report from the same JSON facts."""
    summary = report.get("summary", {})
    lines: list[str] = [
        "# Eventide 运行故障报告",
        "",
        f"- 报告格式版本：{report.get('report_version')}",
        f"- 事实来源：{report.get('source')}",
        f"- Run：`{report.get('run_id')}`",
        f"- 事件高水位：session_seq ≤ {report.get('high_water')}",
        f"- 状态：{_STATUS_LABELS.get(str(report.get('status')), str(report.get('status')))}",
        "",
        "## 发生了什么",
        "",
        str(summary.get("what_happened", "")),
        "",
        "## 停在哪里",
        "",
        str(summary.get("where_stopped") or "正常结束，没有停驻点。"),
        "",
        "## 完成了什么",
        "",
        str(summary.get("completed", "")),
        "",
        "## 哪些结果未知",
        "",
    ]
    unknowns = summary.get("unknowns") or []
    if unknowns:
        lines.extend(f"- {item}" for item in unknowns)
    else:
        lines.append("没有记录到结果未知的项目。")
    lines.extend(["", "## 已知原因", ""])
    causes = report.get("known_causes") or []
    if causes:
        for cause in causes:
            detail = f"：{cause['detail']}" if cause.get("detail") else ""
            lines.append(f"- 【{cause['label']}】{detail}")
    else:
        lines.append("事件中没有记录可直接解释的原因。")
    lines.extend(["", "## 待查事项", ""])
    pending = report.get("pending") or []
    if pending:
        for item in pending:
            detail = f"：{item['detail']}" if item.get("detail") else ""
            lines.append(f"- 【{item['label']}】{detail}")
    else:
        lines.append("没有待查事项。")
    lines.extend(["", "## 下一步可做什么", ""])
    lines.extend(f"- {item}" for item in summary.get("next_steps") or [])
    lines.extend(["", "## 工具调用", ""])
    tools = report.get("tools") or []
    if tools:
        lines.append("| 调用 | 工具 | 状态 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for tool in tools:
            detail = (tool.get("detail") or "").replace("|", "\\|")
            lines.append(
                f"| `{_cell(tool.get('call_id'))}` | {_cell(tool.get('name'))} "
                f"| {tool.get('status')} | {detail} |"
            )
    else:
        lines.append("没有工具调用记录。")
    lines.extend(["", "## 模型请求", ""])
    attempts = report.get("attempts") or []
    if attempts:
        lines.append("| attempt | 序号 | 类型 | 状态 | 用量来源 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in attempts:
            lines.append(
                f"| `{_cell(row.get('attempt_id'))}` | {_cell(row.get('attempt'))} "
                f"| {_cell(row.get('kind'))} | {row.get('status')} | {row.get('usage_source')} |"
            )
    else:
        lines.append("没有模型请求记录。")
    unavailable = report.get("unavailable") or []
    if unavailable:
        lines.extend(["", "## 缺失与不可用", ""])
        lines.extend(f"- {item}" for item in unavailable)
    notes = report.get("notes") or []
    if notes:
        lines.append("")
        lines.append("---")
        lines.append("".join(f"{note} " for note in notes).strip())
    lines.append("")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    text = "未知" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")
