// Pure, disposable presentation facts. Runtime status always comes from the API.
export const terminal = (run) => ["completed", "failed", "interrupted"].includes(run?.status);
export const runId = (run) => run?.run_id || run?.id;
export const eventType = (type) => ({"tool.request": "tool.prepared", "tool.result": "tool.completed"}[type] || type);
export const labels = {
  idle: "尚未开始", running: "正在执行", waiting_for_user: "需要审批",
  parked: "已停驻", interrupted: "已停驻", completed: "本次执行结束", failed: "本次执行失败",
};
export const short = (value, limit = 100) => {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
};

// Why a run parked, in user language. `reason` is the runtime's structured code from
// the terminal event; the raw error text stays available in the Inspector. Labels
// stay neutral: "cancelled" also covers runs cancelled by Host shutdown, so the UI
// never claims who stopped the run.
const parkLabels = {
  step_budget: "步数预算用尽",
  task_step_budget: "单任务步数超限",
  cancelled: "执行已停止",
};
const parkTexts = {
  step_budget: "本次执行用完了步数预算；已完成的工作已保存，可以从当前进度继续。",
  task_step_budget: "同一条任务占用了过多步数，执行已暂停；可以先调整计划，再从该任务继续。",
  cancelled: "这次执行被停止；已完成的工作已保存，可以继续。",
};
const hostStopped = "Host stopped before terminal fact";
export function parkSummary(run) {
  if (run?.status !== "interrupted") return null;
  const error = typeof run.error === "string" ? run.error.trim() : "";
  if (run.reason && parkTexts[run.reason]) return parkTexts[run.reason];
  const gap = Array.isArray(run.gap_files) ? run.gap_files : [];
  if (!run.reason && gap.length) {
    const names = gap.slice(0, 3).join("、");
    return `服务在执行结束前停止，之后有 ${gap.length} 个文件被改动（${names}${gap.length > 3 ? " 等" : ""}）；工作区无法验证，只能放弃恢复或自行确认。`;
  }
  if (!run.reason && (!error || error === hostStopped)) {
    return "服务在执行结束前停止；确认工作区状态后可以继续。";
  }
  return error || "执行被中断；可以尝试继续。";
}
export function parkLabel(run) {
  if (run?.status !== "interrupted") return null;
  if (parkLabels[run.reason]) return parkLabels[run.reason];
  const error = typeof run.error === "string" ? run.error.trim() : "";
  return !run.reason && (!error || error === hostStopped) ? "服务中断" : "执行中断";
}

export function runActivity(run, events = [], now = Date.now() / 1000) {
  const own = events.filter((event) => event.run_id === runId(run));
  const steps = Math.max(run?.steps || 0, ...own.filter((event) => event.type === "model.response").map((event) => event.payload?.step || 0));
  const lastActivity = Math.max(run?.last_activity_at || run?.started_at || now, ...own.map((event) => event.ts || 0));
  return {
    steps,
    elapsedSeconds: Math.max(0, now - (run?.started_at || now)),
    idleSeconds: Math.max(0, now - lastActivity),
  };
}

export function mergeEvents(existing, incoming) {
  const events = new Map(existing.map((e) => [e.event_id || `${e.run_id}:${e.session_seq ?? e.seq}`, e]));
  for (const event of incoming) {
    const normalized = {...event, type: eventType(event.type), seq: event.session_seq ?? event.seq};
    events.set(event.event_id || `${event.run_id}:${normalized.seq}`, normalized);
  }
  return [...events.values()].sort((a, b) => (a.session_seq ?? a.seq) - (b.session_seq ?? b.seq));
}

export function chapters(runs) {
  const byRun = new Map();
  const result = [];
  for (const run of runs) {
    let chapter = byRun.get(run.continuation_of);
    if (!chapter) {
      chapter = {id: runId(run), runs: []};
      result.push(chapter);
    }
    chapter.runs.push(run);
    byRun.set(runId(run), chapter);
  }
  return result;
}

// Task plan display facts. The server's TaskPlanProjection stays the only business
// rule owner; here we only split its task_state payload into visible groups and
// describe evidence as what happened, never as verified success.
const planStatusLabels = {pending: "待开始", in_progress: "进行中", completed: "已完成", blocked: "受阻"};
export function planGroups(taskState) {
  const tasks = taskState?.tasks || [];
  const counts = {completed: 0, in_progress: 0, pending: 0, blocked: 0};
  for (const task of tasks) if (task.status in counts) counts[task.status]++;
  const settled = [];
  const open = [];
  for (const task of tasks) {
    const evidence = (task.evidence || []).map((entry) => ({
      failed: !!entry.is_error,
      callId: entry.call_id || "",
      runId: entry.run_id || "",
      text: `${entry.name || "unknown"}${entry.is_error ? " · 结果为错误" : ""}`,
    }));
    const row = {
      id: task.id || task.content,
      content: task.content || "",
      label: planStatusLabels[task.status] || task.status,
      summary: task.summary || "",
      evidence,
      evidenceOmitted: task.evidence_omitted || 0,
    };
    if (task.status === "in_progress" && task.id) row.active = true;
    (task.status === "completed" ? settled : open).push(row);
  }
  return {counts, total: tasks.length, activeId: taskState?.active_task_id || null, open, settled};
}

function category(name, args) {
  if (["read_file", "glob"].includes(name)) return "explore";
  if (["write_file", "edit_file"].includes(name)) return "modify";
  if (name === "compact") return "context";
  // Only recognize a command invocation, never words inside arbitrary shell strings.
  if (name === "bash" && /^(?:uv run\s+)?(?:python(?:3)? -m\s+)?(?:pytest|ruff|mypy|eventide eval)(?:\s|$)/.test((args.command || "").trim())) return "validate";
  return "tools";
}

const operationLabels = {prepared: "等待结果", unknown: "中断时结果未确认", completed: "已完成", failed: "失败", denied: "已拒绝", abandoned: "已放弃，未重放"};
export function operationText(op) {
  const target = op.arguments.path || op.arguments.pattern || op.arguments.command || op.name;
  return `${short(target, 120)} · ${operationLabels[op.status]}`;
}

export function projectWork(runs, eventMap, taskContents = new Map()) {
  const operations = new Map();
  const approvals = new Map();
  const blocks = [];
  const allEvents = mergeEvents([], runs.flatMap((r) => eventMap.get(runId(r)) || [])).filter((e) => !e.partial);
  const records = new Map(runs.map((r) => [runId(r), r]));
  let intent = "";
  let intentTs = null;
  let checkpoint = null;
  const findOperation = (id, callId) => {
    while (id) {
      const op = operations.get(`${id}:${callId}`);
      if (op) return op;
      id = records.get(id)?.continuation_of;
    }
    return null;
  };
  const note = (event, kind, title, text) => {
    blocks.push({id: event.event_id || `${event.run_id}:${event.seq}`, kind, title, text, runId: event.run_id, events: [event], operations: []});
  };
  for (const event of allEvents) {
    const p = event.payload || {};
    const type = event.type;
    if (type === "message.user" || (type === "message.imported" && p.message?.role === "user")) {
      if (typeof p.message?.content === "string") { intent ||= p.message.content; intentTs ||= event.ts; }
    } else if (type === "run.started" && p.continuation_of) {
      const resumed = p.resumed_task_id ? taskContents.get(p.resumed_task_id) : "";
      note(event, "continuation", "接续上次停驻",
        resumed ? `继续执行任务 ${p.resumed_task_id}：${short(resumed, 80)}。未重发原始指令。`
          : "同一项工作继续执行，未重发原始指令。");
    } else if (type === "workspace.checkpoint") {
      checkpoint = p.checkpoint || null;
    } else if (type === "context.trimmed") {
      const parts = [];
      if (Array.isArray(p.call_ids) && p.call_ids.length) parts.push(`折叠了 ${p.call_ids.length} 条较早的工具结果`);
      if (Array.isArray(p.previewed_call_ids) && p.previewed_call_ids.length) parts.push(`${p.previewed_call_ids.length} 条大型工具结果只随请求发送头尾预览`);
      if (p.elided_plan_updates) parts.push(`${p.elided_plan_updates} 次历史任务计划只随请求发送空清单`);
      if (parts.length) note(event, "context", "已折叠或省略较早的请求内容", `为控制上下文，${parts.join("；")}；原文仍保留在事件日志与工作记录中。`);
    } else if (type === "recovery.abandoned") {
      note(event, "attention", "已放弃本次恢复", "历史记录仍然保留；结果未知的操作没有被当作成功或重新执行。");
    } else if (type === "tool.prepared") {
      const op = {id: `${event.run_id}:${p.call_id}`, callId: p.call_id, runId: event.run_id, name: p.name,
        arguments: p.arguments || {}, status: "prepared", events: [event], result: ""};
      operations.set(op.id, op);
      const kind = category(op.name, op.arguments);
      let block = blocks.at(-1);
      if (!block || block.kind !== kind || block.runId !== event.run_id) {
        block = {id: op.id, kind, runId: event.run_id, events: [], operations: []};
        blocks.push(block);
      }
      block.operations.push(op);
    } else if (type === "tool.completed" || type === "tool.abandoned") {
      let op = findOperation(event.run_id, p.call_id);
      if (!op) {
        // A model-advertised call may be abandoned before tool.prepared exists.
        const lineage = new Set();
        let ancestor = event.run_id;
        while (ancestor && !lineage.has(ancestor)) { lineage.add(ancestor); ancestor = records.get(ancestor)?.continuation_of; }
        const source = [...allEvents].reverse().find((e) => lineage.has(e.run_id) && e.seq < event.seq && e.type === "model.response" &&
          (e.payload.message?.content || []).some?.((b) => b.type === "tool_use" && b.id === p.call_id));
        const advertised = source?.payload.message.content.find((b) => b.type === "tool_use" && b.id === p.call_id);
        op = {id: `${source?.run_id || event.run_id}:${p.call_id}`, callId: p.call_id,
          runId: source?.run_id || event.run_id, name: p.name, arguments: advertised?.input || {}, status: "prepared", result: "", events: source ? [source] : []};
        operations.set(op.id, op);
        blocks.push({id: op.id, kind: "tools", runId: event.run_id, events: [], operations: [op]});
      }
      op.events.push(event);
      op.result = p.content || "";
      op.status = type === "tool.abandoned" ? "abandoned" : op.status === "denied" || (p.is_error && /^Permission denied/.test(op.result)) ? "denied" : p.is_error ? "failed" : "completed";
    } else if (type === "approval.required") {
      approvals.set(p.approval_id, event);
      note(event, "approval", "请求一次授权", p.reason || "此操作需要你的决定。");
    } else if (type === "approval.resolved") {
      const required = approvals.get(p.approval_id);
      // Older approval events have no call_id. Execution is serial, so the nearest
      // prepared operation before that approval is its request evidence.
      const preceding = required && [...operations.values()].reverse().find((candidate) =>
        candidate.runId === required.run_id && candidate.events[0].seq < required.seq &&
        candidate.name === required.payload.tool);
      const op = required && (findOperation(required.run_id, required.payload.call_id) || preceding);
      if (op) { op.events.push(event); if (!p.approved) op.status = "denied"; }
      const block = blocks.find((b) => b.events.includes(required));
      if (block) { block.events.push(event); block.title = p.approved ? "已允许一次操作" : "未授权操作"; }
    } else if (type === "model.retry") {
      note(event, "attention", "模型请求遇到问题", p.retryable ? "请求遇到可重试错误，已保留尝试记录。" : "此错误不可重试，请查看详情。");
    } else if (type === "mcp.connection_failed" || type === "mcp.close_failed") {
      note(event, "attention", type === "mcp.connection_failed" ? "部分 MCP 服务不可用" : "MCP 服务关闭异常",
        type === "mcp.connection_failed" ? `${p.server || "未知服务"} 未连接；其余能力仍可继续使用。` : "本次工作结果已保留，请查看详情。");
    } else if (type === "run.interrupted" || type === "run.failed") {
      note(event, "attention", type === "run.interrupted" ? "执行中断，工作已停驻" : "本次执行失败", p.error || "查看详情了解原因。");
    }
  }
  for (const op of operations.values()) {
    if (op.status === "prepared" && records.get(op.runId)?.status === "interrupted") op.status = "unknown";
  }
  for (const block of blocks) {
    if (!block.operations.length) continue;
    const ops = block.operations;
    const counts = (status) => ops.filter((op) => op.status === status).length;
    const done = counts("completed");
    block.title = {explore: "读取与查找", modify: "文件操作", validate: "运行检查", context: "整理上下文", tools: "工具操作"}[block.kind];
    const targets = [...new Set(ops.map((op) => op.arguments.path || op.arguments.pattern).filter(Boolean))];
    block.text = [targets.length ? targets.map((p) => short(p, 70)).slice(0, 3).join("、") : "",
      done ? `${done} 项操作完成` : "", counts("prepared") ? `${counts("prepared")} 项等待结果` : "",
      counts("unknown") ? `${counts("unknown")} 项中断时结果未确认` : "",
      counts("failed") ? `${counts("failed")} 项失败` : "", counts("denied") ? `${counts("denied")} 项拒绝` : "",
      counts("abandoned") ? `${counts("abandoned")} 项已放弃` : ""].filter(Boolean).join(" · ");
    block.events = mergeEvents([], ops.flatMap((op) => op.events));
  }
  return {intent, intentTs, blocks, operations, events: allEvents, checkpoint};
}

// ---- v2 panel facts: pure mappings shared by the right-hand panel renders ----

// Bucket counts for one chapter's operations (`projectWork().operations` values).
// A future status still counts toward `total` but never toward an invented bucket.
export function toolStats(operations) {
  const stats = {total: 0, completed: 0, failed: 0, denied: 0, prepared: 0, unknown: 0, abandoned: 0};
  for (const op of operations || []) {
    if (!op || !(op.status in stats)) continue;
    stats.total++;
    stats[op.status]++;
  }
  return stats;
}

// Composer/usage context pressure. Either input missing or nonsensical means "no
// measurement yet", never a fabricated 0%. The result is a clamped 0-100 integer.
export function contextPct(requestChars, limit) {
  const number = (value) => typeof value === "number" && Number.isFinite(value);
  if (!number(requestChars) || requestChars < 0 || !number(limit) || limit <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((requestChars / limit) * 100)));
}

const terminalLabels = {"run.completed": "本次执行结束", "run.failed": "本次执行失败", "run.interrupted": "执行中断"};

// One node per timeline-worthy event. `runs` supplies the interrupted-run facts that
// turn a still-prepared call into "unknown" instead of a fake ongoing execution.
export function timeline(events, runs = []) {
  const interrupted = new Set((runs || []).filter((run) => run?.status === "interrupted").map((run) => runId(run)));
  const nodes = [];
  let countedSteps = 0;
  for (const event of events || []) {
    if (!event || event.partial) continue;
    const p = event.payload || {};
    const type = eventType(event.type);
    const seq = event.session_seq ?? event.seq;
    if (type === "model.request") {
      const step = Number.isFinite(p.step) ? p.step : ++countedSteps;
      nodes.push({kind: "step", seq, label: `第 ${step} 步`, status: "completed", ts: event.ts});
    } else if (type === "tool.prepared") {
      nodes.push({kind: "tool", seq, label: p.name || "工具调用", status: interrupted.has(event.run_id) ? "unknown" : "prepared", ts: event.ts});
    } else if (type === "tool.completed") {
      const content = typeof p.content === "string" ? p.content : "";
      const status = p.is_error && /^Permission denied/.test(content) ? "denied" : p.is_error ? "failed" : "completed";
      nodes.push({kind: "tool", seq, label: p.name || "工具调用", status, ts: event.ts});
    } else if (type === "tool.abandoned") {
      nodes.push({kind: "tool", seq, label: p.name || "工具调用", status: "unknown", ts: event.ts});
    } else if (type === "approval.required") {
      nodes.push({kind: "approval", seq, label: p.tool || "需要审批", status: "pending", ts: event.ts});
    } else if (type === "approval.resolved") {
      const status = p.auto ? "auto" : p.approved ? "approved" : "denied";
      nodes.push({kind: "approval", seq, label: p.tool || "需要审批", status, ts: event.ts});
    } else if (type === "workspace.checkpoint") {
      nodes.push({kind: "checkpoint", seq, label: "记录工作区 checkpoint", status: "completed", ts: event.ts});
    } else if (type === "run.started" && p.continuation_of) {
      nodes.push({kind: "continue", seq, label: "接续上次停驻", status: "completed", ts: event.ts});
    } else if (terminalLabels[type]) {
      nodes.push({kind: "terminal", seq, label: terminalLabels[type], status: type.slice(4), ts: event.ts});
    }
  }
  return nodes.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
}

// task.plan_updated snapshots, in order. Legacy todo items without an id cannot be
// referenced later, so they stay out of the revision history.
export function planRevisions(events) {
  const revisions = [];
  for (const event of events || []) {
    if (!event || event.partial || eventType(event.type) !== "task.plan_updated") continue;
    const todos = Array.isArray(event.payload?.todos) ? event.payload.todos : [];
    revisions.push({
      seq: event.session_seq ?? event.seq,
      step: typeof event.payload?.step === "number" ? event.payload.step : null,
      todos: todos.filter((todo) => todo && todo.id),
    });
  }
  return revisions;
}
