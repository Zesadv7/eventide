const $ = (selector) => document.querySelector(selector);

const state = {
  configured: false,
  workspaces: [],
  currentWorkspaceId: localStorage.getItem("nexus.workspace") || null,
  sessions: [],
  currentSessionId: localStorage.getItem("nexus.session") || null,
  messages: new Map(),
  runs: new Map(),
  events: new Map(),
  cursors: new Map(),
  connections: new Map(),
  retries: new Map(),
  inspector: null,
  continueTask: null,
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "interrupted"]);
const EVENT_TYPES = [
  "message.user", "message.imported", "run.started", "context.configured", "context.compacted",
  "model.request", "model.response", "model.retry", "tool.request", "tool.result",
  "tool.prepared", "tool.completed", "tool.abandoned", "workspace.checkpoint",
  "approval.required", "approval.resolved", "run.interrupted", "run.failed", "run.completed",
];

const STATUS_LABELS = {
  idle: "Idle", running: "Running", waiting_for_user: "Waiting",
  parked: "Parked", interrupted: "Parked", completed: "Completed", failed: "Failed",
};

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* text response */ }
    throw new Error(detail);
  }
  return response.json();
}

function normalizeRun(run) {
  return { ...run, run_id: run.run_id || run.id };
}

function currentWorkspace() {
  return state.workspaces.find((workspace) => workspace.workspace_id === state.currentWorkspaceId);
}

function currentSession() {
  return state.sessions.find((session) => session.session_id === state.currentSessionId);
}

function currentRuns() {
  return state.runs.get(state.currentSessionId) || [];
}

function normalizeEventType(type) {
  return { "tool.request": "tool.prepared", "tool.result": "tool.completed" }[type] || type;
}

function runStatus(run) {
  if (!run) return "idle";
  return run.status === "interrupted" ? "parked" : run.status;
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  return new Date(timestamp * 1000).toLocaleString("zh-CN", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function excerpt(value, length = 160) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function setHealth(online, text) {
  $("#health").textContent = text;
  $("#health-dot").classList.toggle("online", online);
}

function renderWorkspaceBar() {
  const workspace = currentWorkspace();
  $("#current-workspace-name").textContent = workspace?.name || "未选择";
  $("#current-workspace-git").textContent = workspace?.git_branch
    ? `${workspace.git_branch} · ${(workspace.git_head || "").slice(0, 7)}`
    : workspace?.git_head ? `detached · ${workspace.git_head.slice(0, 7)}` : "非 Git Workspace";

  const switcher = $("#workspace-switcher");
  switcher.replaceChildren();
  for (const item of state.workspaces) {
    const option = document.createElement("option");
    option.value = item.workspace_id;
    option.textContent = item.name;
    option.selected = item.workspace_id === state.currentWorkspaceId;
    switcher.append(option);
  }
}

function renderSessionList() {
  const list = $("#session-list");
  list.replaceChildren();
  if (!state.sessions.length) {
    const empty = document.createElement("p");
    empty.className = "nav-empty";
    empty.textContent = "还没有 Session。";
    list.append(empty);
    return;
  }

  const groups = [
    ["RUNNING", state.sessions.filter((s) => ["running", "waiting_for_user"].includes(s.status))],
    ["PARKED", state.sessions.filter((s) => s.status === "parked")],
    ["RECENT", state.sessions.filter((s) => !["running", "waiting_for_user", "parked"].includes(s.status))],
  ];
  for (const [label, sessions] of groups) {
    if (!sessions.length) continue;
    const heading = document.createElement("div");
    heading.className = "session-group-label";
    heading.textContent = label;
    list.append(heading);
    for (const session of sessions) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = `session-item${session.session_id === state.currentSessionId ? " selected" : ""}`;
      item.onclick = () => selectSession(session.session_id);
      const marker = document.createElement("span");
      marker.className = `session-marker status-${runStatus(session.latest_run)}`;
      const copy = document.createElement("span");
      copy.className = "session-item-copy";
      const title = document.createElement("strong");
      title.textContent = session.title || "New session";
      const meta = document.createElement("small");
      meta.textContent = `${statusLabel(session.status)}${session.updated_at ? ` · ${formatTime(session.updated_at)}` : ""}`;
      copy.append(title, meta);
      item.append(marker, copy);
      list.append(item);
    }
  }
}

function renderSessionHeader() {
  const session = currentSession();
  const run = session?.latest_run;
  $("#session-title").textContent = session?.title || "选择一个 Session";
  $("#session-meta").textContent = session
    ? `${session.session_id} · ${run ? `Last run ${normalizeRun(run).run_id}` : "No runs yet"}`
    : "Session 的工作过程会在这里持续展开。";
  const status = session?.status || "idle";
  $("#session-status").textContent = statusLabel(status);
  $("#session-status").className = `session-status status-${runStatus(session?.latest_run)}`;
  $("#composer-hint").textContent = session
    ? status === "parked" ? "Continue 这个 Session，或创建新的工作" : "继续这个工作上下文"
    : "选择 Session 后可开始";
}

function classifyTool(name, argumentsValue = {}) {
  if (["read_file", "glob"].includes(name)) return "explore";
  if (["write_file", "edit_file"].includes(name)) return "modify";
  if (name === "compact") return "context";
  if (name === "bash" && /\b(pytest|ruff|mypy|nexus-agent\s+eval|git\s+diff\s+--check|check|test|lint|typecheck)\b/i.test(argumentsValue.command || "")) {
    return "validate";
  }
  return "tools";
}

function blockDetail(block) {
  if (block.kind === "explore") {
    const files = [...block.files];
    return files.length ? `${block.actions} actions · ${files.length} paths` : `${block.actions} read/search actions`;
  }
  if (block.kind === "modify") {
    const files = [...block.files];
    return files.length ? `${files.length} files · ${block.completed} completed` : `${block.completed} completed`;
  }
  if (block.kind === "validate") return `${block.completed} checks · ${block.errors ? `${block.errors} failed` : "no errors reported"}`;
  if (block.kind === "tools") return `${block.actions} calls${block.errors ? ` · ${block.errors} failed` : ""}`;
  if (block.kind === "context") return "Context rebuilt or compacted";
  if (block.kind === "attention") return block.detail;
  return block.detail || "";
}

function createBlock(kind, label, event) {
  return {
    id: `${kind}-${event?.seq || Math.random().toString(16).slice(2)}`,
    kind, label, detail: "", events: event ? [event] : [],
    actions: 0, completed: 0, errors: 0, files: new Set(), warning: false,
  };
}

function addBlock(blocks, kind, label, event) {
  const last = blocks[blocks.length - 1];
  if (last && last.kind === kind && !["terminal", "approval", "checkpoint", "intent"].includes(kind)) {
    if (event) last.events.push(event);
    return last;
  }
  const block = createBlock(kind, label, event);
  blocks.push(block);
  return block;
}

function projectRun(runId) {
  const events = state.events.get(runId) || [];
  const blocks = [];
  for (const event of events) {
    const type = normalizeEventType(event.type);
    const payload = event.payload || {};
    if (["message.user", "message.imported"].includes(type)) {
      const message = payload.message || {};
      const block = addBlock(blocks, "intent", "Intent", event);
      block.detail = excerpt(message.content);
      continue;
    }
    if (["context.configured", "context.compacted"].includes(type)) {
      const block = addBlock(blocks, "context", "Preparing context", event);
      block.detail = type === "context.compacted" ? "Context compacted" : "Context prepared";
      continue;
    }
    if (type === "model.retry") {
      const last = blocks[blocks.length - 1] || addBlock(blocks, "attention", "Model attention", event);
      last.warning = true;
      last.detail = "Model retry · inspect for details";
      last.events.push(event);
      continue;
    }
    if (type === "tool.prepared" || type === "tool.completed" || type === "tool.abandoned") {
      const category = type === "tool.abandoned" ? "attention" : classifyTool(payload.name, payload.arguments || {});
      const labels = { explore: "Exploring workspace", modify: "Updated workspace", validate: "Running validation", context: "Preparing context", tools: "Tool activity", attention: "Recovery attention" };
      const block = addBlock(blocks, category, labels[category], event);
      if (type === "tool.prepared") block.actions += 1;
      if (type === "tool.completed") {
        block.completed += 1;
        if (payload.is_error) block.errors += 1;
      }
      if (type === "tool.abandoned") { block.warning = true; block.errors += 1; }
      const path = payload.arguments?.path;
      if (path) block.files.add(path);
      block.detail = blockDetail(block);
      continue;
    }
    if (type === "workspace.checkpoint") {
      const block = addBlock(blocks, "checkpoint", "Workspace checkpoint", event);
      block.detail = "Workspace evidence recorded";
      continue;
    }
    if (type === "approval.required" || type === "approval.resolved") {
      const block = addBlock(blocks, "approval", type === "approval.required" ? "Approval required" : "Approval resolved", event);
      block.detail = type === "approval.required" ? payload.reason || "User approval is required" : payload.approved ? "Allowed once" : "Not allowed";
      continue;
    }
    if (["run.interrupted", "run.failed", "run.completed"].includes(type)) {
      const labels = { "run.interrupted": "Parked", "run.failed": "Failed", "run.completed": "Completed" };
      const block = addBlock(blocks, "terminal", labels[type], event);
      block.detail = payload.error || payload.output || "";
    }
  }
  return blocks;
}

function renderNarrative() {
  const narrative = $("#narrative");
  narrative.replaceChildren();
  const runs = currentRuns();
  if (!runs.length) {
    const empty = document.createElement("div");
    empty.className = "narrative-empty";
    empty.innerHTML = '<span class="empty-line"></span><p>还没有运行。描述下一步要完成的工作。</p>';
    narrative.append(empty);
    return;
  }
  runs.forEach((rawRun, index) => {
    const run = normalizeRun(rawRun);
    const item = document.createElement("article");
    item.className = "run-spine-item";
    const spine = document.createElement("div");
    spine.className = `spine-node status-${runStatus(run)}`;
    const body = document.createElement("div");
    body.className = "run-body";
    const header = document.createElement("div");
    header.className = "run-header";
    const title = document.createElement("button");
    title.type = "button";
    title.className = "run-title";
    title.textContent = `Run ${index + 1}`;
    title.onclick = () => openInspector(run.run_id);
    const status = document.createElement("span");
    status.className = `run-status status-${runStatus(run)}`;
    status.textContent = statusLabel(runStatus(run));
    const time = document.createElement("time");
    time.textContent = formatTime(run.started_at);
    header.append(title, status, time);
    if (run.continuation_of) {
      const continuation = document.createElement("p");
      continuation.className = "continuation-note";
      continuation.textContent = `Continued from ${run.continuation_of}`;
      body.append(continuation);
    }
    const blocks = projectRun(run.run_id);
    if (!blocks.length && run.status === "running") {
      const preparing = createBlock("context", "Preparing context");
      preparing.detail = "Waiting for the first runtime facts";
      blocks.push(preparing);
    }
    const blockList = document.createElement("div");
    blockList.className = "execution-blocks";
    for (const block of blocks) {
      const blockButton = document.createElement("button");
      blockButton.type = "button";
      blockButton.className = `execution-block kind-${block.kind}${block.warning ? " has-warning" : ""}`;
      const blockTitle = document.createElement("strong");
      blockTitle.textContent = block.label;
      const blockDetailEl = document.createElement("span");
      blockDetailEl.textContent = blockDetail(block);
      blockButton.append(blockTitle, blockDetailEl);
      blockButton.onclick = () => openInspector(run.run_id, block);
      blockList.append(blockButton);
    }
    body.append(header, blockList);
    if (run.status === "interrupted") {
      const continueButton = document.createElement("button");
      continueButton.type = "button";
      continueButton.className = "continue-button";
      continueButton.textContent = "Continue this Session";
      continueButton.onclick = () => continueSession(run.run_id);
      body.append(continueButton);
    }
    if (run.output && run.status === "completed") {
      const result = document.createElement("div");
      result.className = "assistant-result";
      const resultLabel = document.createElement("span");
      resultLabel.className = "result-label";
      resultLabel.textContent = "Result";
      const resultText = document.createElement("p");
      resultText.textContent = run.output;
      result.append(resultLabel, resultText);
      body.append(result);
    }
    item.append(spine, body);
    narrative.append(item);
  });
}

function openInspector(runId, block = null) {
  state.inspector = { runId, block };
  renderInspector();
  $("#inspector").classList.add("open");
  $("#inspector").setAttribute("aria-hidden", "false");
}

function renderInspector() {
  const inspector = $("#inspector-content");
  inspector.replaceChildren();
  if (!state.inspector) return;
  const { runId, block } = state.inspector;
  const events = state.events.get(runId) || [];
  const run = currentRuns().map(normalizeRun).find((item) => item.run_id === runId);
  $("#inspector-title").textContent = block?.label || `Run ${runId}`;
  const summary = document.createElement("p");
  summary.className = "inspector-summary";
  summary.textContent = block ? blockDetail(block) : `${statusLabel(runStatus(run))} · ${events.length} runtime facts`;
  inspector.append(summary);

  const list = document.createElement("div");
  list.className = "event-detail-list";
  const selectedEvents = block?.events?.length ? block.events : events;
  for (const event of selectedEvents) {
    const detail = document.createElement("details");
    detail.className = "event-detail-item";
    const summaryEl = document.createElement("summary");
    summaryEl.innerHTML = `<span>${normalizeEventType(event.type)}</span><time>${formatTime(event.ts)}</time>`;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(event.payload || {}, null, 2);
    detail.append(summaryEl, pre);
    list.append(detail);
  }
  inspector.append(list);
}

function closeInspector() {
  state.inspector = null;
  $("#inspector").classList.remove("open");
  $("#inspector").setAttribute("aria-hidden", "true");
}

function parseSse(text) {
  return text.split(/\n\n+/).flatMap((chunk) => {
    const lines = chunk.split(/\n/);
    const event = { type: "message", data: "", id: null };
    for (const line of lines) {
      if (line.startsWith("event:")) event.type = line.slice(6).trim();
      if (line.startsWith("id:")) event.id = Number(line.slice(3).trim());
      if (line.startsWith("data:")) event.data += line.slice(5).trim();
    }
    if (!event.data) return [];
    try { return [{ ...JSON.parse(event.data), type: event.type, seq: event.id || JSON.parse(event.data).seq }]; }
    catch (_) { return []; }
  });
}

async function loadRunEvents(runId) {
  if (state.events.has(runId)) return state.events.get(runId);
  const response = await fetch(`/api/runs/${runId}/events?after=0`, { headers: { Accept: "text/event-stream" } });
  if (!response.ok) throw new Error(`Unable to load events for ${runId}`);
  const events = parseSse(await response.text());
  state.events.set(runId, events);
  if (events.length) state.cursors.set(runId, Math.max(...events.map((event) => event.seq || 0)));
  return events;
}

function handleEvent(runId, event) {
  const existing = state.events.get(runId) || [];
  if (event.event_id && existing.some((item) => item.event_id === event.event_id)) return;
  if (!event.event_id && existing.some((item) => item.seq === event.seq)) return;
  existing.push(event);
  existing.sort((a, b) => (a.seq || 0) - (b.seq || 0));
  state.events.set(runId, existing);
  state.cursors.set(runId, Math.max(state.cursors.get(runId) || 0, event.seq || 0));
  renderNarrative();
  if (["run.completed", "run.failed", "run.interrupted"].includes(normalizeEventType(event.type))) settleRun(runId);
}

function closeSubscription(runId) {
  const connection = state.connections.get(runId);
  if (connection) connection.close();
  state.connections.delete(runId);
  const timer = state.retries.get(runId);
  if (timer) clearTimeout(timer);
  state.retries.delete(runId);
}

function subscribeRun(runId) {
  closeSubscription(runId);
  const after = state.cursors.get(runId) || 0;
  const source = new EventSource(`/api/runs/${runId}/events?after=${after}`);
  state.connections.set(runId, source);
  source.onopen = () => { state.retries.delete(runId); renderSessionList(); };
  const receive = (message) => {
    try { handleEvent(runId, { ...JSON.parse(message.data), type: message.type, seq: Number(message.lastEventId || 0) || undefined }); }
    catch (_) { /* malformed event is ignored; durable state remains queryable */ }
  };
  for (const type of EVENT_TYPES) source.addEventListener(type, receive);
  source.onmessage = receive;
  source.onerror = async () => {
    source.close();
    state.connections.delete(runId);
    try {
      const run = normalizeRun(await request(`/api/runs/${runId}`));
      if (TERMINAL_STATUSES.has(run.status)) { await settleRun(runId); return; }
    } catch (_) { /* retry below */ }
    const delay = Math.min(5000, 500 * 2 ** ((state.retries.get(runId) || 0)));
    state.retries.set(runId, (state.retries.get(runId) || 0) + 1);
    $("#composer-hint").textContent = "连接暂时中断，正在恢复事件流…";
    const timer = setTimeout(() => subscribeRun(runId), delay);
    state.retries.set(runId, timer);
  };
}

async function settleRun(runId) {
  try {
    const run = normalizeRun(await request(`/api/runs/${runId}`));
    const runs = currentRuns().map(normalizeRun);
    const index = runs.findIndex((item) => item.run_id === runId);
    if (index >= 0) runs[index] = run;
    state.runs.set(state.currentSessionId, runs);
    closeSubscription(runId);
    await loadSessions(false);
    renderSessionHeader();
    renderNarrative();
  } catch (_) { /* next refresh will reconcile */ }
}

async function loadSessions(select = true) {
  if (!state.currentWorkspaceId) return;
  state.sessions = await request(`/api/workspaces/${state.currentWorkspaceId}/sessions`);
  renderSessionList();
  renderWorkspaceBar();
  syncWorkspaceSubscriptions();
  if (!select) return;
  const preferred = state.sessions.find((session) => session.session_id === state.currentSessionId);
  if (preferred) return selectSession(preferred.session_id);
  if (state.sessions[0]) return selectSession(state.sessions[0].session_id);
  return createSession();
}

async function syncWorkspaceSubscriptions() {
  const active = new Set();
  for (const session of state.sessions) {
    const latest = session.latest_run ? normalizeRun(session.latest_run) : null;
    if (latest && ["running", "waiting_for_user"].includes(latest.status)) {
      active.add(latest.run_id);
      if (!state.connections.has(latest.run_id)) subscribeRun(latest.run_id);
    }
  }
  for (const runId of state.connections.keys()) if (!active.has(runId)) closeSubscription(runId);
}

async function selectWorkspace(workspaceId) {
  if (!workspaceId) return;
  state.currentWorkspaceId = workspaceId;
  localStorage.setItem("nexus.workspace", workspaceId);
  state.currentSessionId = null;
  localStorage.removeItem("nexus.session");
  for (const runId of state.connections.keys()) closeSubscription(runId);
  renderWorkspaceBar();
  await loadSessions(true);
}

async function selectSession(sessionId) {
  const session = state.sessions.find((item) => item.session_id === sessionId);
  if (!session) return;
  state.currentSessionId = sessionId;
  localStorage.setItem("nexus.session", sessionId);
  closeInspector();
  renderSessionHeader();
  try {
    const [messages, rawRuns] = await Promise.all([
      request(`/api/sessions/${sessionId}/messages`).catch(() => []),
      request(`/api/sessions/${sessionId}/runs`),
    ]);
    state.messages.set(sessionId, messages);
    const runs = rawRuns.map(normalizeRun);
    state.runs.set(sessionId, runs);
    await Promise.all(runs.filter((run) => TERMINAL_STATUSES.has(run.status)).map((run) => loadRunEvents(run.run_id)));
    renderSessionHeader();
    renderNarrative();
    syncWorkspaceSubscriptions();
  } catch (error) {
    $("#narrative").innerHTML = `<div class="narrative-empty"><p>${error.message}</p></div>`;
  }
  $(".session-nav").classList.remove("mobile-open");
}

async function createSession() {
  if (!state.currentWorkspaceId) return;
  const result = await request("/api/sessions", {
    method: "POST", body: JSON.stringify({ workspace_id: state.currentWorkspaceId }),
  });
  await loadSessions(false);
  await selectSession(result.session_id);
}

async function startRun(prompt) {
  if (!state.currentSessionId) await createSession();
  if (!state.currentSessionId) return;
  if (!state.configured) { $("#model-dialog").showModal(); setConfigMessage("请先配置 API Key"); return; }
  $("#run-button").disabled = true;
  $("#composer-hint").textContent = "Run 正在启动…";
  try {
    const accepted = await request(`/api/sessions/${state.currentSessionId}/runs`, {
      method: "POST", body: JSON.stringify({ prompt }),
    });
    state.events.delete(accepted.run_id);
    state.cursors.delete(accepted.run_id);
    await loadSessions(false);
    subscribeRun(accepted.run_id);
    await selectSession(state.currentSessionId);
  } finally {
    $("#run-button").disabled = false;
  }
}

async function continueSession(previousRunId) {
  if (state.continueTask) return;
  const sessionId = state.currentSessionId;
  const oldRunId = previousRunId;
  $("#composer-hint").textContent = "正在恢复 Session…";
  let settled = false;
  const task = request(`/api/sessions/${sessionId}/continue`, { method: "POST" })
    .finally(() => { settled = true; });
  state.continueTask = task;
  try {
    let discovered = null;
    while (!settled && !discovered) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      const session = await request(`/api/sessions/${sessionId}`);
      const latest = session.latest_run ? normalizeRun(session.latest_run) : null;
      if (latest && latest.run_id !== oldRunId) discovered = latest;
    }
    const result = normalizeRun(await task);
    discovered = discovered || result;
    await loadSessions(false);
    if (discovered?.run_id) {
      state.events.delete(discovered.run_id);
      state.cursors.delete(discovered.run_id);
      subscribeRun(discovered.run_id);
    }
    await selectSession(sessionId);
  } catch (error) {
    alert(error.message);
    await loadSessions(false);
    await selectSession(sessionId);
  } finally {
    state.continueTask = null;
    $("#composer-hint").textContent = "继续这个工作上下文";
  }
}

function providerBody() {
  const body = { provider: $("#provider").value, model: $("#model").value.trim(), base_url: $("#base-url").value.trim() || null };
  const key = $("#api-key").value.trim();
  if (key) body.api_key = key;
  return body;
}

function setConfigMessage(text, success = false) {
  const message = $("#config-message");
  message.textContent = text;
  message.className = `config-message${success ? " success" : ""}`;
}

function renderProviderStatus(config) {
  state.configured = config.api_key_configured;
  $("#model-state").textContent = config.configuration_error ? "Error" : state.configured ? config.model : "Needs setup";
  $("#model-config-button").classList.toggle("configured", state.configured);
  $("#provider").value = config.provider;
  $("#model").value = config.model || "";
  $("#base-url").value = config.base_url || "";
  $("#key-hint").textContent = config.api_key_status === "decrypt_error" ? config.configuration_error : state.configured ? "安全保存的 API Key 已配置" : "尚未保存 API Key";
}

async function loadProviderConfig() { renderProviderStatus(await request("/api/config/provider")); }

async function saveProviderConfig(event) {
  event.preventDefault();
  $("#save-model-config").disabled = true;
  setConfigMessage("正在保存…");
  try {
    const config = await request("/api/config/provider", { method: "PUT", body: JSON.stringify(providerBody()) });
    $("#api-key").value = "";
    renderProviderStatus(config);
    setConfigMessage("配置已保存并生效", true);
  } catch (error) { setConfigMessage(error.message); }
  finally { $("#save-model-config").disabled = false; }
}

async function testProviderConfig() {
  if (!$("#model-form").reportValidity()) return;
  const result = $("#connection-result");
  result.hidden = false;
  result.textContent = "正在检查连接…";
  try {
    const probe = await request("/api/config/provider/test", { method: "POST", body: JSON.stringify(providerBody()) });
    result.textContent = `${probe.message} · ${Math.round(probe.latency_ms)} ms`;
    result.className = `connection-result ${probe.success ? "success" : "failure"}`;
  } catch (error) { result.textContent = error.message; result.className = "connection-result failure"; }
}

async function clearApiKey() {
  if (!confirm("确定清除已保存的 API Key 吗？")) return;
  try {
    const body = { ...providerBody(), clear_api_key: true };
    delete body.api_key;
    renderProviderStatus(await request("/api/config/provider", { method: "PUT", body: JSON.stringify(body) }));
    $("#api-key").value = "";
    setConfigMessage("已清除数据库中的 API Key", true);
  } catch (error) { setConfigMessage(error.message); }
}

async function resetProviderConfig() {
  if (!confirm("确定恢复服务启动时的配置吗？")) return;
  try { renderProviderStatus(await request("/api/config/provider", { method: "DELETE" })); setConfigMessage("已恢复启动配置", true); }
  catch (error) { setConfigMessage(error.message); }
}

async function bootstrap() {
  try {
    await request("/healthz");
    setHealth(true, "Online");
    await loadProviderConfig();
    state.workspaces = await request("/api/workspaces");
    renderWorkspaceBar();
    const selected = state.workspaces.some((item) => item.workspace_id === state.currentWorkspaceId)
      ? state.currentWorkspaceId : state.workspaces[0]?.workspace_id;
    if (selected) await selectWorkspace(selected);
  } catch (error) {
    setHealth(false, "Offline");
    $("#narrative").innerHTML = `<div class="narrative-empty"><p>${error.message}</p></div>`;
  }
}

$("#workspace-switcher").addEventListener("change", (event) => selectWorkspace(event.target.value));
$("#new-session").addEventListener("click", () => createSession().catch((error) => alert(error.message)));
$("#prompt-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = $("#prompt").value.trim();
  if (!prompt) return;
  try { await startRun(prompt); $("#prompt").value = ""; }
  catch (error) { alert(error.message); }
});
$("#prompt").addEventListener("keydown", (event) => { if (event.ctrlKey && event.key === "Enter") $("#prompt-form").requestSubmit(); });
$("#close-inspector").addEventListener("click", closeInspector);
$("#model-config-button").addEventListener("click", () => $("#model-dialog").showModal());
$("#close-model-dialog").addEventListener("click", () => $("#model-dialog").close());
$("#model-form").addEventListener("submit", saveProviderConfig);
$("#test-model-config").addEventListener("click", testProviderConfig);
$("#clear-api-key").addEventListener("click", clearApiKey);
$("#reset-model-config").addEventListener("click", resetProviderConfig);
$("#toggle-key").addEventListener("click", () => {
  const input = $("#api-key");
  input.type = input.type === "password" ? "text" : "password";
  $("#toggle-key").textContent = input.type === "password" ? "Show" : "Hide";
});
$("#mobile-open-nav").addEventListener("click", () => $(".session-nav").classList.add("mobile-open"));
$("#mobile-close-nav").addEventListener("click", () => $(".session-nav").classList.remove("mobile-open"));

bootstrap();
