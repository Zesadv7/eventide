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

export function projectWork(runs, eventMap) {
  const operations = new Map();
  const approvals = new Map();
  const blocks = [];
  const allEvents = mergeEvents([], runs.flatMap((r) => eventMap.get(runId(r)) || [])).filter((e) => !e.partial);
  const records = new Map(runs.map((r) => [runId(r), r]));
  let intent = "";
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
      if (typeof p.message?.content === "string") intent ||= p.message.content;
    } else if (type === "run.started" && p.continuation_of) {
      note(event, "continuation", "接续上次停驻", "同一项工作继续执行，未重发原始指令。");
    } else if (type === "workspace.checkpoint") {
      checkpoint = p.checkpoint || null;
    } else if (type === "context.trimmed") {
      const count = (p.call_ids || []).length;
      note(event, "context", "已折叠较早的工具结果", `为控制上下文，折叠了 ${count} 条较早的工具结果；原文仍保留在事件日志与工作记录中。`);
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
  return {intent, blocks, operations, events: allEvents, checkpoint};
}
