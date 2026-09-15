import {chapters, contextPct, eventType, labels, mergeEvents, operationText, parkSummary, planGroups, projectWork, runActivity, runId, short, terminal, timeline, toolStats} from "./projection.js?v=12";
import {EventFeed, request} from "./transport.js?v=12";
import {el, button, icon, reconcile, markdown} from "./view.js?v=14";

// config.js is null-safe for nodes the v2 console dropped, so a failed import is
// the only remaining degrade path: the app keeps running as "unconfigured".
const config = await import("./config.js?v=13").catch(() => null);

const $ = (selector) => document.querySelector(selector);
const state = {
  workspace: localStorage.getItem("eventide.workspace"), session: null, workspaces: [],
  sessions: new Map(), runs: new Map(), expanded: new Map(), manualExpansion: new Set(), errors: new Map(), notices: new Map(),
  drafts: new Map(), positions: new Map(), busy: new Set(), approvals: new Set(),
  hasOlder: new Map(), loadingOlder: new Set(), showArchived: new Set(), sessionEditing: null, planCompletedOpen: false,
  reconnecting: new Set(), workCache: new Map(), openToolDetails: new Map(), openToolGroups: new Set(), runNumbers: new Map(),
  detail: {chapterId: null, runId: null, target: null, eventSeq: null, filter: "all"},
  attachments: new Map(), attachmentsUnavailable: false, attachmentsToasted: false,
  providerDefaultModel: "", availableModels: [],
  capabilities: new Map(), usageData: null, usageRunId: null,
};
const sessionJobs = new Map(), workspaceJobs = new Map(), loadingChapters = new Map();
const sessionRecord = (id = state.session) => [...state.sessions.values()].flat().find((s) => s.session_id === id);
const taskContents = (id = state.session) => new Map((sessionRecord(id)?.task_state?.tasks || []).filter((t) => t.id).map((t) => [t.id, t.content]));
const currentRuns = () => state.runs.get(state.session) || [];
const time = (timestamp) => timestamp ? new Date(timestamp * 1000).toLocaleString("zh-CN", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "";
const duration = (seconds) => seconds < 60 ? `${Math.floor(seconds)} 秒` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒` : `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
const title = (session) => session?.title && session.title !== "New session" ? session.title : "新的工作";
const draftKey = () => state.session || `new:${state.workspace}`;
let viewVersion = 0;
let frame;
function scheduleRender() {
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = null; render(); });
}

// ---- toasts: transient facts surface here; only composer errors keep #action-message ----
function showToast(type, message, options = {}) {
  const host = $("#toasts");
  if (!host) return;
  while (host.children.length >= 4) host.firstElementChild.remove();
  const toast = el("div", `toast toast-${type}`);
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  toast.append(el("span", "toast-message", message));
  if (options.retry) toast.append(button("重试", () => { toast.remove(); options.retry(); }, "toast-retry"));
  const close = button("", () => toast.remove(), "toast-close");
  close.setAttribute("aria-label", "关闭提示");
  close.append(icon("close"));
  toast.append(close);
  host.append(toast);
  if (type !== "error") setTimeout(() => toast.isConnected && toast.remove(), 5000);
  return toast;
}
function toastError(error, retry) { showToast("error", error?.message || String(error), retry ? {retry} : {}); }

let runtimeSettings = null;
async function loadSettings() {
  try { runtimeSettings = await request("/api/runtime/settings"); }
  catch { runtimeSettings = {context_limit: 50000}; }
}
const contextLimit = () => typeof runtimeSettings?.context_limit === "number" && runtimeSettings.context_limit > 0 ? runtimeSettings.context_limit : 50000;

const feed = new EventFeed((owner, event) => {
  scheduleRender();
  // task.plan_updated refreshes the authoritative plan from session_status, so the
  // UI never folds plan events itself; other state facts keep their existing cues.
  if (["approval.required", "approval.resolved", "run.started", "run.completed", "run.failed", "run.interrupted", "task.plan_updated", "stream.settled"].includes(event.type)) {
    void refreshSession(owner).catch((error) => toastError(error, () => void refreshSession(owner).catch(() => {})));
  }
}, (owner, message) => {
  if (message) {
    if (!state.reconnecting.has(owner)) showToast("error", message, {retry: () => void refreshSession(owner).catch(() => {})});
    state.reconnecting.add(owner);
  } else {
    state.reconnecting.delete(owner);
  }
  renderPill();
});

function report(owner, error) { state.errors.set(owner, error.message || String(error)); scheduleRender(); }
function subscriptions() {
  const owners = new Set((state.sessions.get(state.workspace) || []).map((s) => s.session_id));
  // Continue requests remain observable even when the user opens another workspace.
  for (const owner of state.busy) if (sessionRecord(owner)) owners.add(owner);
  feed.retain(owners);
  for (const owner of owners) {
    const run = sessionRecord(owner)?.latest_run;
    if (run && !terminal(run)) feed.watch(runId(run), owner);
  }
}

async function refreshWorkspace(id) {
  if (workspaceJobs.has(id)) return workspaceJobs.get(id);
  const suffix = state.showArchived.has(id) ? "?include_archived=true" : "";
  const job = request(`/api/workspaces/${id}/sessions${suffix}`).then((sessions) => {
    state.sessions.set(id, sessions.sort((a, b) => b.updated_at - a.updated_at || b.created_at - a.created_at));
    subscriptions(); scheduleRender();
  }).finally(() => workspaceJobs.delete(id));
  workspaceJobs.set(id, job);
  return job;
}

async function refreshSession(id) {
  if (!id) return;
  if (sessionJobs.has(id)) { sessionJobs.get(id).again = true; return sessionJobs.get(id).promise; }
  const job = {again: false};
  job.promise = (async () => {
    do {
      job.again = false;
      const [session, page] = await Promise.all([request(`/api/sessions/${id}`), request(`/api/sessions/${id}/runs?limit=101`)]);
      const hasExisting = state.runs.has(id);
      const hasOlder = page.length > 100;
      const recent = hasOlder ? page.slice(1) : page;
      const known = state.runs.get(id) || [];
      const merged = new Map([...known, ...recent].map((run) => [runId(run), run]));
      const runs = [...merged.values()].sort((a, b) => a.started_at - b.started_at);
      if (!hasExisting) state.hasOlder.set(id, hasOlder);
      const list = state.sessions.get(session.workspace_id) || [];
      state.sessions.set(session.workspace_id, [...list.filter((s) => s.session_id !== id), session].sort((a, b) => b.updated_at - a.updated_at));
      state.runs.set(id, runs);
      subscriptions();
      if (id === state.session) {
        const groups = chapters(runs);
        if (groups.length && !state.expanded.has(groups.at(-1).id)) {
          for (const chapter of groups.slice(0, -1)) if (!state.manualExpansion.has(chapter.id)) state.expanded.set(chapter.id, false);
          state.expanded.set(groups.at(-1).id, true);
        }
        if (!state.detail.chapterId || !groups.some((g) => g.id === state.detail.chapterId)) {
          state.detail = {...state.detail, chapterId: groups.at(-1)?.id || null, runId: null, target: null, eventSeq: null};
        }
        for (const chapter of groups) if (state.expanded.get(chapter.id)) void loadChapter(id, chapter).catch((error) => report(id, error));
      }
      scheduleRender();
    } while (job.again);
  })().finally(() => sessionJobs.delete(id));
  sessionJobs.set(id, job);
  return job.promise;
}

async function loadOlderRuns() {
  const owner = state.session;
  const runs = currentRuns();
  if (!owner || !runs.length || state.loadingOlder.has(owner)) return;
  state.loadingOlder.add(owner); scheduleRender();
  try {
    const before = encodeURIComponent(runId(runs[0]));
    const page = await request(`/api/sessions/${owner}/runs?limit=101&before=${before}`);
    const hasOlder = page.length > 100;
    const older = hasOlder ? page.slice(1) : page;
    const merged = new Map([...older, ...currentRuns()].map((run) => [runId(run), run]));
    state.runs.set(owner, [...merged.values()].sort((a, b) => a.started_at - b.started_at));
    state.hasOlder.set(owner, hasOlder);
  } catch (error) { toastError(error, () => void loadOlderRuns()); }
  finally { state.loadingOlder.delete(owner); scheduleRender(); }
}

async function loadChapter(owner, chapter) {
  if (loadingChapters.has(chapter.id)) return loadingChapters.get(chapter.id);
  const job = Promise.all(chapter.runs.map((run) => terminal(run) ? feed.history(runId(run), owner) : feed.watch(runId(run), owner)))
    .finally(() => { loadingChapters.delete(chapter.id); scheduleRender(); });
  loadingChapters.set(chapter.id, job);
  return job;
}

// projectWork is pure but not free; chapter facts only change when the chapter's
// event count, run count or task_state revision changes.
function projectCached(chapter) {
  let eventCount = 0;
  for (const run of chapter.runs) eventCount += (feed.events.get(runId(run)) || []).length;
  const session = sessionRecord();
  const key = `${chapter.id}|${eventCount}|${chapter.runs.length}|${session?.task_state?.updated_seq || 0}`;
  const hit = state.workCache.get(chapter.id);
  if (hit && hit.key === key) return hit.work;
  const work = projectWork(chapter.runs, feed.events, taskContents());
  state.workCache.set(chapter.id, {key, work});
  return work;
}

function scrollHost() {
  let node = document.querySelector(".document");
  while (node && node !== document.body) {
    if (node.scrollHeight > node.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(node).overflowY)) return node;
    node = node.parentElement;
  }
  return document.scrollingElement;
}
function rememberView() {
  state.drafts.set(draftKey(), $("#prompt").value);
  const host = scrollHost();
  if (state.session && host) state.positions.set(state.session, host.scrollTop);
}
function resetPromptHeight() {
  const prompt = $("#prompt");
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 200)}px`;
}

async function selectSession(id) {
  viewVersion++;
  rememberView();
  state.session = id;
  if (id) {
    localStorage.setItem(`eventide.session.${state.workspace}`, id);
    localStorage.setItem("eventide.session", id);
  }
  $("#prompt").value = state.drafts.get(draftKey()) || "";
  resetPromptHeight();
  state.workCache.clear(); state.openToolDetails.clear(); state.openToolGroups.clear();
  state.detail = {...state.detail, chapterId: null, runId: null, target: null, eventSeq: null};
  state.usageData = null; state.usageRunId = null;
  $("#sidebar").classList.remove("open"); syncDrawerBackdrop();
  render();
  const host = scrollHost();
  if (host) host.scrollTop = state.positions.get(id) || 0;
  if (id) await Promise.all([
    refreshSession(id).catch((error) => toastError(error, () => void refreshSession(id).catch(() => {}))),
    loadPendingAttachments(id),
  ]);
}
async function selectWorkspace(id) {
  rememberView();
  const version = ++viewVersion;
  state.workspace = id; state.session = null;
  $("#prompt").value = state.drafts.get(draftKey()) || "";
  resetPromptHeight();
  localStorage.setItem("eventide.workspace", id);
  state.workCache.clear(); state.openToolDetails.clear(); state.openToolGroups.clear();
  state.detail = {...state.detail, chapterId: null, runId: null, target: null, eventSeq: null};
  state.usageData = null; state.usageRunId = null;
  closeWorkspaceMenu();
  render();
  try {
    await refreshWorkspace(id);
    if (state.workspace !== id || version !== viewVersion) return;
    const sessions = state.sessions.get(id) || [];
    const remembered = localStorage.getItem(`eventide.session.${id}`) || localStorage.getItem("eventide.session");
    await selectSession(sessions.find((s) => s.session_id === remembered)?.session_id || sessions[0]?.session_id || null);
  } catch (error) { toastError(error, () => void selectWorkspace(id)); }
}

async function createSession(workspace = state.workspace) {
  const version = viewVersion;
  const result = await request("/api/sessions", {method: "POST", body: JSON.stringify({workspace_id: workspace})});
  await refreshSession(result.session_id);
  if (state.workspace === workspace && viewVersion === version) await selectSession(result.session_id);
  return result.session_id;
}

// An explicit user action must claim the view synchronously. A still-settling
// auto-select tail from an earlier action (workspace add, initial load) bumps
// viewVersion when it lands; without this claim that late bump would make
// createSession's staleness guard silently skip selecting the new session.
function claimView() { viewVersion++; }

async function addWorkspace(event) {
  event.preventDefault();
  const path = $("#workspace-input").value.trim();
  if (!path) return;
  $("#save-workspace").disabled = true;
  $("#workspace-message").textContent = "";
  try {
    const workspace = await request("/api/workspaces", {method: "POST", body: JSON.stringify({path})});
    state.workspaces = [...state.workspaces.filter((item) => item.workspace_id !== workspace.workspace_id), workspace];
    $("#workspace-dialog").close();
    $("#workspace-input").value = "";
    await selectWorkspace(workspace.workspace_id);
  } catch (error) { $("#workspace-message").textContent = error.message || String(error); }
  finally { $("#save-workspace").disabled = false; scheduleRender(); }
}

function closeWorkspaceMenu() {
  $("#workspace-menu").hidden = true;
  $("#workspace-switcher-button").setAttribute("aria-expanded", "false");
}
function renderWorkspaceMenu() {
  reconcile($("#workspace-options"), state.workspaces, (w) => w.workspace_id, (w) => JSON.stringify([w.name, w.path, w.workspace_id === state.workspace]), (w) => {
    const option = el("li", "workspace-option");
    option.dataset.workspaceId = w.workspace_id;
    option.append(el("span", "workspace-option-name", w.name || "工作区"), el("span", "workspace-option-path", w.path || ""));
    option.onclick = () => { closeWorkspaceMenu(); void selectWorkspace(w.workspace_id); };
    return option;
  });
}
async function removeCurrentWorkspace() {
  const id = state.workspace;
  if (!id || state.busy.has(`remove:${id}`)) return;
  if (!confirm("移除这个工作区吗？只有没有任何会话的工作区可以被移除。")) return;
  closeWorkspaceMenu();
  state.busy.add(`remove:${id}`);
  try {
    await request(`/api/workspaces/${id}`, {method: "DELETE"});
    state.workspaces = state.workspaces.filter((w) => w.workspace_id !== id);
    showToast("info", "工作区已移除。");
    const next = state.workspaces[0]?.workspace_id;
    if (next) await selectWorkspace(next);
    else { state.workspace = null; state.session = null; render(); }
  } catch (error) { showToast("error", error.message || "移除失败。", {retry: () => void removeCurrentWorkspace()}); }
  finally { state.busy.delete(`remove:${id}`); scheduleRender(); }
}

function openSessionActions(session, event) {
  event?.preventDefault();
  event?.stopPropagation();
  state.sessionEditing = session.session_id;
  $("#session-title-input").value = title(session);
  $("#session-working-directory").textContent = `工作目录：${session.working_directory || "Workspace 根目录"}`;
  $("#archive-session").textContent = session.archived ? "恢复到列表" : "归档";
  $("#session-message").textContent = "";
  $("#session-dialog").showModal();
  $("#session-title-input").focus();
  $("#session-title-input").select();
}

async function saveSessionTitle(event) {
  event.preventDefault();
  const session = sessionRecord(state.sessionEditing);
  const value = $("#session-title-input").value.trim();
  if (!session || !value) return;
  $("#save-session").disabled = true;
  try {
    await request(`/api/sessions/${session.session_id}`, {method: "PATCH", body: JSON.stringify({title: value})});
    $("#session-dialog").close();
    await refreshWorkspace(session.workspace_id);
    if (state.session === session.session_id) await refreshSession(session.session_id);
  } catch (error) { $("#session-message").textContent = error.message || String(error); }
  finally { $("#save-session").disabled = false; scheduleRender(); }
}

async function toggleSessionArchive() {
  const session = sessionRecord(state.sessionEditing);
  if (!session) return;
  $("#archive-session").disabled = true;
  try {
    await request(`/api/sessions/${session.session_id}`, {method: "PATCH", body: JSON.stringify({archived: !session.archived})});
    $("#session-dialog").close();
    await refreshWorkspace(session.workspace_id);
    if (!session.archived && state.session === session.session_id && !state.showArchived.has(session.workspace_id)) {
      const remaining = state.sessions.get(session.workspace_id) || [];
      await selectSession(remaining[0]?.session_id || null);
    }
  } catch (error) { $("#session-message").textContent = error.message || String(error); }
  finally { $("#archive-session").disabled = false; scheduleRender(); }
}

async function deleteEmptySession() {
  const session = sessionRecord(state.sessionEditing);
  if (!session || !confirm("删除这条空工作记录吗？有运行历史的记录不会被删除。")) return;
  $("#delete-session").disabled = true;
  try {
    await request(`/api/sessions/${session.session_id}`, {method: "DELETE"});
    state.runs.delete(session.session_id);
    state.hasOlder.delete(session.session_id);
    $("#session-dialog").close();
    await refreshWorkspace(session.workspace_id);
    if (state.session === session.session_id) {
      localStorage.removeItem(`eventide.session.${session.workspace_id}`);
      const remaining = state.sessions.get(session.workspace_id) || [];
      await selectSession(remaining[0]?.session_id || null);
    }
  } catch (error) { $("#session-message").textContent = error.message || String(error); }
  finally { $("#delete-session").disabled = false; scheduleRender(); }
}

// ---- shared fact rendering (kept from the former inspector) ----
function factsNode(facts) {
  const section = el("div");
  for (const [key, value] of Object.entries(facts)) {
    section.append(el("h3", "", key), typeof value === "string" ? el("p", "", value) : el("pre", "", JSON.stringify(value, null, 2)));
  }
  return section;
}
function rawEventNode(event) {
  const details = el("details", "raw-event");
  const id = event.event_id || `${event.run_id}:${event.session_seq ?? event.seq}`;
  details.dataset.disclosure = id;
  details.append(el("summary", "", `${event.type} · ${time(event.ts)}`), el("pre", "", JSON.stringify(event, null, 2)));
  return details;
}

// ---- sidebar ----
function renderNavigation() {
  const workspace = state.workspaces.find((w) => w.workspace_id === state.workspace);
  $("#workspace-name").textContent = workspace?.name || "工作空间";
  $("#workspace-git").textContent = workspace?.git_head ? `${workspace.git_branch || "detached"} · ${workspace.git_head.slice(0, 7)}` : workspace?.git_root ? "尚无可用 Git 提交" : "非 Git 工作区";
  $("#workspace-path").textContent = workspace?.path || "";
  $("#crumb-workspace").textContent = workspace?.name || "工作空间";
  const session = sessionRecord();
  $("#crumb-session").textContent = session ? title(session) : "未选择工作";
  renderSessionList();
  $("#new-session").disabled = !state.workspace || state.busy.has(`new:${state.workspace}`);
  $("#toggle-archived").textContent = state.showArchived.has(state.workspace) ? "隐藏已归档" : "显示已归档";
}

function sessionGroupKey(session) {
  const startOfToday = new Date(); startOfToday.setHours(0, 0, 0, 0);
  const updated = (session.updated_at || 0) * 1000;
  if (updated >= startOfToday.getTime()) return "today";
  if (updated >= startOfToday.getTime() - 7 * 86400000) return "week";
  return "older";
}
const sessionGroupLabels = {today: "今天", week: "7 天内", older: "更早"};
function sessionItem(session) {
  const item = el("div", "session-item");
  item.dataset.sessionId = session.session_id;
  item.setAttribute("aria-current", String(session.session_id === state.session));
  item.oncontextmenu = (event) => openSessionActions(session, event);
  const select = button("", () => void selectSession(session.session_id), "session-select");
  const meta = el("span", "session-meta");
  const dot = el("span", "status-dot");
  dot.dataset.status = session.status;
  dot.title = labels[session.status] || session.status;
  meta.append(dot);
  const plan = planGroups(session.task_state);
  if (plan.total) {
    const badge = el("span", "plan-badge");
    badge.dataset.done = String(plan.counts.completed);
    badge.dataset.total = String(plan.total);
    badge.textContent = `${plan.counts.completed}/${plan.total}`;
    meta.append(badge);
  }
  meta.append(el("time", "", `${session.archived ? "已归档 · " : ""}${time(session.updated_at)}`));
  select.append(el("strong", "", title(session)), meta);
  const more = button("", (event) => openSessionActions(session, event), "session-more");
  more.append(icon("dots"));
  item.append(select, more);
  return item;
}
function renderSessionList() {
  const sessions = state.sessions.get(state.workspace) || [];
  const buckets = new Map([["today", []], ["week", []], ["older", []]]);
  for (const session of sessions) buckets.get(sessionGroupKey(session)).push(session);
  const visible = [...buckets.entries()].filter(([, list]) => list.length);
  reconcile($("#session-list"), visible, ([key]) => key, ([key, list]) => `${key}:${list.map((s) => JSON.stringify([s.title, s.status, s.updated_at, s.archived, s.working_directory, s.session_id === state.session, planGroups(s.task_state).total])).join("|")}`, ([key, list]) => {
    const group = el("div", "session-group");
    group.dataset.group = key;
    group.append(el("div", "session-group-label", sessionGroupLabels[key]));
    for (const session of list) group.append(sessionItem(session));
    return group;
  });
}

// ---- document body ----
function renderPlan(session) {
  const plan = session ? planGroups(session.task_state) : null;
  const section = $("#plan");
  if (!plan || !plan.total) {
    section.replaceChildren(el("p", "", "本次工作没有任务计划。"));
    return;
  }
  const counts = plan.counts;
  const version = JSON.stringify([plan, state.planCompletedOpen]);
  reconcile(section, [version], (key) => key, () => version, () => {
    const node = el("div");
    const completedOpen = state.planCompletedOpen;
    const rows = [...plan.open, {planGroup: "completed", open: completedOpen, count: plan.settled.length, rows: plan.settled}];
    reconcile(node, rows, (r) => r.id || r.planGroup, (r) => JSON.stringify(r), (row) => {
      if (row.planGroup === "completed") {
        const details = el("details", "plan-completed");
        details.open = row.open;
        details.addEventListener("toggle", () => { state.planCompletedOpen = details.open; });
        details.append(el("summary", "", `已完成的任务 (${row.count})`));
        for (const task of row.rows) details.append(planRow(task, true));
        return details;
      }
      return planRow(row, false);
    });
    const heading = el("div", "section-heading");
    const parts = [`${counts.completed} / ${plan.total} 已完成`];
    if (counts.in_progress) parts.push(`${counts.in_progress} 进行中`);
    if (counts.blocked) parts.push(`${counts.blocked} 受阻`);
    heading.append(el("span", "plan-counts", parts.join(" · ")));
    node.prepend(heading);
    return node;
  });
}

function planRow(task, settled) {
  const item = el("div", `plan-task status-${settled ? "completed" : task.label === "受阻" ? "blocked" : task.active ? "active" : "open"}`);
  const head = el("div", "plan-task-head");
  head.append(el("span", "plan-status", task.label), el("span", "plan-content", task.content));
  item.append(head);
  if (task.summary) item.append(el("p", "plan-summary", task.summary));
  if (task.evidence.length || task.evidenceOmitted) {
    const details = el("details", "plan-evidence");
    details.append(el("summary", "", `工作证据 (${task.evidence.length}${task.evidenceOmitted ? `，另有 ${task.evidenceOmitted} 条未列出` : ""})`));
    for (const entry of task.evidence) {
      const failed = entry.failed ? " failed" : "";
      const target = entry.runId && entry.callId ? `${entry.runId}:${entry.callId}` : null;
      if (target) {
        const line = button(entry.text, () => openToolAt(target), `plan-evidence-item${failed}`);
        line.title = "在工具列表中查看这次调用";
        line.dataset.panelOpen = "";
        details.append(line);
      } else {
        details.append(el("div", `plan-evidence-item${failed}`, entry.text));
      }
    }
    details.append(el("p", "plan-evidence-note", "证据只记录发生过的工具调用，不代表验证通过。"));
    item.append(details);
  }
  return item;
}

function renderApprovals(session) {
  const run = session?.latest_run;
  const pending = run && !terminal(run) ? Object.entries(run.pending_approvals || {}) : [];
  const owner = session?.session_id;
  reconcile($("#approvals"), pending, ([id]) => id, ([id, p]) => JSON.stringify([p, state.approvals.has(id)]), ([id, p]) => {
    const section = el("div", "approval");
    section.append(el("h3", "", "这一步需要你的决定"), el("p", "", p.reason || "工具请求一次授权。"), el("pre", "", `${p.tool}\n${JSON.stringify(p.arguments || {}, null, 2)}`));
    const actions = el("div", "approval-actions");
    for (const [text, approved] of [["本次允许", true], ["拒绝", false]]) {
      const action = button(text, () => void resolveApproval(owner, runId(run), id, approved), approved ? "primary-button" : "secondary-button");
      action.disabled = state.approvals.has(id); action.dataset.focus = `${id}:${approved}`; actions.append(action);
    }
    section.append(actions); return section;
  });
}

async function resolveApproval(owner, id, approvalId, approved) {
  if (state.approvals.has(approvalId)) return;
  state.approvals.add(approvalId); scheduleRender();
  try {
    const run = await request(`/api/runs/${id}`);
    if (terminal(run) || !run.pending_approvals?.[approvalId]) throw new Error("此审批已结束，请查看更新后的工作状态。");
    await request(`/api/runs/${id}/approvals/${approvalId}`, {method: "POST", body: JSON.stringify({approved})});
    state.errors.delete(owner);
  } catch (error) { toastError(error, () => void resolveApproval(owner, id, approvalId, approved)); }
  finally { await refreshSession(owner).catch(() => {}); state.approvals.delete(approvalId); scheduleRender(); }
}

// ---- chapter narrative ----
function buildUserBubble(item) {
  const bubble = el("div", "user-bubble");
  bubble.append(el("div", "user-bubble-meta", item.ts ? `你 · ${time(item.ts)}` : "你"));
  bubble.append(el("div", "user-bubble-text", item.text));
  const copy = button("复制", async () => {
    try { await navigator.clipboard.writeText(item.text); copy.textContent = "已复制"; }
    catch { copy.textContent = "复制失败"; }
  }, "user-bubble-copy");
  bubble.append(copy);
  return bubble;
}
// 每轮工作的最终回复：内联轻卡片，头部小字带 run 定位、步数与复制。
function buildResultCard(item) {
  const card = el("section", "chapter-result");
  const head = el("div", "chapter-result-head");
  const runButton = el("button", "result-run", `run#${state.runNumbers.get(runId(item.run)) || "?"}`);
  runButton.type = "button";
  runButton.title = "在详情浮层查看这次执行";
  runButton.dataset.panelOpen = "";
  runButton.onclick = () => openDetailAtRun(item.chapterId, runId(item.run));
  const seconds = runDurationSeconds(item.run);
  head.append(runButton, el("span", "", `${item.run.steps || 0} 步${seconds == null ? "" : ` · ${duration(seconds)}`}`), el("time", "", time(item.run.started_at)));
  const copy = button("复制", async () => {
    try { await navigator.clipboard.writeText(item.text); copy.textContent = "已复制"; }
    catch { copy.textContent = "复制失败，请选择文本复制"; }
  }, "result-copy");
  head.append(copy);
  card.append(head, markdown(item.text));
  return card;
}
function buildStatsLine(item) {
  const line = el("button", "chapter-stats-line");
  line.type = "button";
  const parts = [];
  for (const block of item.phases) {
    const dot = el("span", "stat-dot");
    dot.dataset.kind = block.kind;
    line.append(dot);
    const counts = toolStats(block.operations);
    const problem = counts.failed ? `${counts.failed} 项失败` : counts.denied ? `${counts.denied} 项拒绝` :
      counts.unknown ? `${counts.unknown} 项未确认` : counts.abandoned ? `${counts.abandoned} 项放弃` : "";
    parts.push(problem ? `${block.title} ${problem}` : `${block.title} ${counts.total}`);
  }
  line.append(document.createTextNode(parts.join(" · ")));
  line.title = "在工具面板中查看本章调用";
  line.dataset.panelOpen = "";
  line.onclick = () => openToolsForChapter(item.chapterId);
  return line;
}
function buildNoteBlock(item) {
  const node = el("div", "work-block kind-attention");
  node.append(el("h3", "", item.block.title), el("p", "", item.block.text));
  const detailsButton = button("查看详情", () => openDetailAtBlock(item.chapterId, item.block.id), "text-button");
  detailsButton.dataset.panelOpen = "";
  node.append(detailsButton);
  return node;
}
function buildLineage(chapter, runs) {
  const container = el("div", "run-lineage");
  for (const run of runs) {
    const badge = el("button", "lineage-badge");
    badge.type = "button";
    badge.dataset.runId = runId(run);
    badge.dataset.status = run.status;
    badge.textContent = `run#${state.runNumbers.get(runId(run)) || "?"} · ${run.steps || 0} 步 · ${labels[run.status] || run.status}${run.continuation_of ? " · 接续" : ""}`;
    badge.title = "在详情面板查看这次执行";
    badge.dataset.panelOpen = "";
    badge.onclick = () => openDetailAtRun(chapter.id, runId(run));
    container.append(badge);
  }
  return container;
}

function renderHistory(runs) {
  const owner = state.session;
  $("#load-older").hidden = !owner || !state.hasOlder.get(owner);
  $("#load-older").disabled = state.loadingOlder.has(owner);
  $("#load-older").textContent = state.loadingOlder.has(owner) ? "正在加载…" : "加载更早记录";
  const groups = chapters(runs);
  if (!groups.length) {
    reconcile($("#narrative"), [owner || "empty"], (id) => id, () => "empty", () => {
      const emptyText = owner ? "还没有执行记录。描述下一步要完成的工作，记录将在这里持续展开。"
        : state.workspace ? "选择左侧的工作，或直接描述一个目标。首次提交时创建工作记录。"
          : "先添加一个工作区，再描述第一个目标。";
      const wrap = el("div", "empty-state");
      wrap.append(el("p", "empty-work", emptyText));
      const actions = el("div", "empty-actions");
      if (!state.workspace) {
        actions.append(button("添加工作区", () => { $("#add-workspace").click(); }, "secondary-button"));
      } else {
        for (const sample of ["概括这个仓库的结构", "检查测试覆盖并补齐薄弱点", "审查最近的改动并给出建议"]) {
          actions.append(button(sample, () => {
            $("#prompt").value = sample;
            state.drafts.set(draftKey(), sample);
            $("#prompt").focus();
          }, "example-task"));
        }
      }
      wrap.append(actions);
      return wrap;
    });
    return;
  }
  reconcile($("#narrative"), groups, (g) => g.id, (g) => g.id, (g) => {
    const details = el("details", "chapter"); details.dataset.disclosure = g.id;
    details.open = state.expanded.get(g.id) ?? g.id === groups.at(-1).id;
    details.append(el("summary", "chapter-summary"), el("div", "chapter-body"));
    details.firstElementChild.addEventListener("click", () => state.manualExpansion.add(g.id));
    details.addEventListener("toggle", () => {
      state.expanded.set(g.id, details.open);
      if (details.open) {
        const fresh = chapters(state.runs.get(owner) || []).find((c) => c.id === g.id);
        if (fresh) void loadChapter(owner, fresh).catch((error) => toastError(error, () => void refreshSession(owner).catch(() => {})));
        state.detail = {...state.detail, chapterId: g.id, runId: null, target: null, eventSeq: null};
        renderDetail(); renderContextMeter();
      }
    });
    return details;
  });
  for (const chapter of groups) {
    const node = [...$("#narrative").children].find((n) => n.dataset.key === chapter.id);
    const expanded = state.expanded.get(chapter.id);
    if (expanded !== undefined && node.open !== expanded) node.open = expanded;
    const work = projectCached(chapter);
    const label = work.intent ? short(work.intent, 80) : chapter.id === groups[0].id ? "最初的目标" : "后续工作";
    const summary = node.firstElementChild;
    const summaryText = `${label} · ${labels[chapter.runs.at(-1).status]} · ${time(chapter.runs[0].started_at)}`;
    if (summary.textContent !== summaryText) summary.textContent = summaryText;
    if (!node.open) continue;
    const items = [];
    if (work.intent) items.push({id: "user", kind: "user", text: work.intent, ts: work.intentTs});
    const phases = work.blocks.filter((block) => block.operations.length);
    if (phases.length) items.push({id: "stats", kind: "stats", phases, chapterId: chapter.id, version: JSON.stringify(phases.map((p) => [p.id, p.title, p.text]))});
    for (const block of work.blocks) if (!block.operations.length) items.push({id: block.id, kind: "note", block, chapterId: chapter.id});
    if (!work.intent && !work.blocks.length) {
      const loaded = chapter.runs.every((r) => feed.loaded.has(runId(r)));
      items.push({id: "empty", kind: "empty", text: terminal(chapter.runs.at(-1)) ? loaded ? "本次执行没有工具活动。" : "正在读取工作记录…" : "正在准备执行，等待新的工作记录。"});
    }
    for (const run of chapter.runs) {
      if (run.output && run.status === "completed") items.push({id: `result:${runId(run)}`, kind: "result", run, chapterId: chapter.id, text: run.output});
    }
    const lineageRuns = chapter.runs.filter((r) => !(r.status === "completed" && r.output));
    if (lineageRuns.length) items.push({id: "lineage", kind: "lineage", chapter, runs: lineageRuns, version: JSON.stringify(lineageRuns.map((r) => [runId(r), r.status, r.steps, r.continuation_of]))});
    reconcile(node.lastElementChild, items, (item) => item.id, (item) => {
      if (item.kind === "user") return JSON.stringify([item.text, item.ts]);
      if (item.kind === "stats" || item.kind === "lineage") return item.version;
      if (item.kind === "note") return JSON.stringify([item.block.title, item.block.text]);
      return item.text || item.id;
    }, (item) => {
      if (item.kind === "user") return buildUserBubble(item);
      if (item.kind === "stats") return buildStatsLine(item);
      if (item.kind === "note") return buildNoteBlock(item);
      if (item.kind === "result") return buildResultCard(item);
      if (item.kind === "lineage") return buildLineage(item.chapter, item.runs);
      return el("p", "", item.text);
    });
  }
}

// ---- floating panel: topbar capsules open one section at a time ----
const CAPSULE_TITLES = {plan: "任务计划", tools: "工具活动", detail: "详情", usage: "用量"};
let openCapsule = null;
function openPanel(name) {
  openCapsule = name;
  renderFloatingPanel();
  $("#floating-panel").focus({preventScroll: true});
}
function closePanel({restoreFocus = true} = {}) {
  const previous = openCapsule;
  openCapsule = null;
  renderFloatingPanel();
  if (restoreFocus && previous) $(`#capsule-${previous}`)?.focus({preventScroll: true});
}
function togglePanel(name) { if (openCapsule === name) closePanel(); else openPanel(name); }
function renderFloatingPanel() {
  $("#floating-panel").hidden = !openCapsule;
  $("#floating-title").textContent = CAPSULE_TITLES[openCapsule] || "";
  for (const name of Object.keys(CAPSULE_TITLES)) {
    $(`#capsule-${name}`).setAttribute("aria-expanded", String(openCapsule === name));
    $(`#panel-${name}`).hidden = openCapsule !== name;
  }
}
function openToolsForChapter(chapterId) {
  openPanel("tools");
  state.openToolGroups.add(chapterId);
  const group = document.querySelector(`#tool-list .tool-group[data-chapter-id="${CSS.escape(chapterId)}"]`);
  if (group) { group.open = true; group.scrollIntoView({block: "start"}); }
}
function openToolAt(opId) {
  openPanel("tools");
  for (const chapter of chapters(currentRuns())) {
    if (projectCached(chapter).operations.has(opId)) {
      state.openToolDetails.set(chapter.id, opId);
      state.openToolGroups.add(chapter.id);
    }
  }
  renderTools();
  document.querySelector(`#tool-list .tool-row[data-op-id="${CSS.escape(opId)}"]`)?.scrollIntoView({block: "nearest"});
}
function openDetailAtBlock(chapterId, blockId) {
  openPanel("detail");
  state.detail = {...state.detail, chapterId, runId: null, target: {type: "block", blockId}, eventSeq: null};
  renderDetail();
}
function openDetailAtRun(chapterId, id) {
  openPanel("detail");
  state.detail = {...state.detail, chapterId, runId: id, target: null, eventSeq: null};
  renderDetail();
}

const toolTarget = (op) => short(op.arguments?.path || op.arguments?.pattern || op.arguments?.command || op.name, 60);
function toolDurationText(op) {
  if (op.status === "prepared") return "运行中";
  const stamps = op.events.map((event) => event.ts).filter((ts) => typeof ts === "number");
  if (stamps.length < 2) return "";
  const seconds = Math.max(0, stamps.at(-1) - stamps[0]);
  return seconds < 1 ? "<1 秒" : duration(seconds);
}
function applyToolDetail(group, item) {
  const openId = state.openToolDetails.get(item.chapter.id);
  const detail = group.querySelector(".tool-detail");
  if (!openId || !detail) return;
  const op = item.operations.find((candidate) => candidate.id === openId);
  if (!op) { state.openToolDetails.delete(item.chapter.id); return; }
  detail.replaceChildren(factsNode({对象: op.arguments, 状态: operationText(op), 结果: op.result || "尚无结果"}), ...op.events.map(rawEventNode));
  detail.hidden = false;
}
function buildToolGroup(item) {
  const group = el("details", "tool-group");
  group.dataset.chapterId = item.chapter.id;
  group.dataset.disclosure = `tool-group:${item.chapter.id}`;
  group.open = state.openToolGroups.has(item.chapter.id);
  group.addEventListener("toggle", () => {
    if (group.open) state.openToolGroups.add(item.chapter.id);
    else state.openToolGroups.delete(item.chapter.id);
  });
  const label = item.work.intent ? short(item.work.intent, 30) : item.first ? "最初的目标" : "后续工作";
  const counts = toolStats(item.operations);
  const problem = counts.failed ? `${counts.failed} 项失败` : counts.unknown ? `${counts.unknown} 项未确认` : counts.denied ? `${counts.denied} 项拒绝` : "";
  group.append(el("summary", "tool-group-label", `${label} · ${item.operations.length} 次${problem ? ` · ${problem}` : ""}`));
  for (const op of item.operations) {
    const row = el("button", "tool-row");
    row.type = "button";
    row.dataset.opId = op.id;
    row.dataset.status = op.status;
    row.dataset.focus = op.id;
    row.append(el("span", "tool-dot"), el("span", "tool-name", op.name || "工具"), el("span", "tool-target", toolTarget(op)), el("time", "tool-duration", toolDurationText(op)));
    row.onclick = () => {
      const current = state.openToolDetails.get(item.chapter.id);
      state.openToolDetails.set(item.chapter.id, current === op.id ? null : op.id);
      renderTools();
      if (current !== op.id) document.querySelector(`#tool-list .tool-row[data-op-id="${CSS.escape(op.id)}"]`)?.scrollIntoView({block: "nearest"});
    };
    group.append(row);
  }
  const detail = el("div", "tool-detail");
  detail.hidden = true;
  group.append(detail);
  applyToolDetail(group, item);
  return group;
}
let lastToolTotals = null;
function renderTools() {
  const stats = $("#tool-stats");
  const list = $("#tool-list");
  const items = [];
  const totals = {total: 0, completed: 0, failed: 0, denied: 0, prepared: 0, unknown: 0, abandoned: 0};
  const groups = chapters(currentRuns());
  for (const [index, chapter] of groups.entries()) {
    const work = projectCached(chapter);
    const operations = [...work.operations.values()];
    if (!operations.length) continue;
    items.push({chapter, work, operations, first: index === 0});
    totals.total += operations.length;
  }
  for (const item of items) {
    for (const [key, value] of Object.entries(toolStats(item.operations))) if (key !== "total") totals[key] += value;
  }
  if (!items.length) {
    lastToolTotals = null;
    stats.hidden = true;
    list.replaceChildren(el("p", "", "还没有工具调用记录。"));
    return;
  }
  lastToolTotals = totals;
  stats.hidden = false;
  stats.replaceChildren(...[["total", "总"], ["completed", "完成"], ["failed", "失败"], ["prepared", "等待"], ["unknown", "未知"]]
    .map(([kind, label]) => {
      const chip = el("span", "stat-chip", `${label} ${totals[kind]}`);
      chip.dataset.kind = kind;
      return chip;
    }));
  reconcile(list, items, (item) => item.chapter.id, (item) => JSON.stringify([
    item.operations.map((op) => [op.id, op.status, op.name, toolTarget(op), toolDurationText(op)]),
    item.first, state.openToolDetails.get(item.chapter.id) || "",
  ]), buildToolGroup);
}

const filterCategories = [["all", "全部"], ["tool", "工具"], ["model", "模型"], ["system", "系统"], ["context", "上下文"]];
const categoryOf = (node) => node.kind === "tool" ? "tool" : node.kind === "step" ? "model" : node.kind === "checkpoint" ? "context" : "system";
function renderFilters() {
  reconcile($("#detail-filters"), filterCategories, ([key]) => key, ([key]) => String(key === state.detail.filter), ([key, label]) => {
    const chip = el("button", "filter-chip");
    chip.type = "button";
    chip.dataset.filter = key;
    chip.textContent = label;
    chip.onclick = () => { state.detail.filter = key; renderDetail(); };
    return chip;
  });
  for (const chip of $("#detail-filters").children) chip.setAttribute("aria-pressed", String(chip.dataset.filter === state.detail.filter));
}
function inLineage(ancestor, descendant) {
  const records = new Map(currentRuns().map((run) => [runId(run), run]));
  let id = descendant;
  while (id) {
    if (id === ancestor) return true;
    id = records.get(id)?.continuation_of;
  }
  return false;
}
function eventFacts(event, work) {
  const p = event.payload || {};
  const type = eventType(event.type);
  if (type.startsWith("tool.")) {
    const op = work.operations.get(`${event.run_id}:${p.call_id}`) ||
      [...work.operations.values()].find((candidate) => candidate.callId === p.call_id && inLineage(candidate.runId, event.run_id));
    if (op) return {对象: op.arguments, 状态: operationText(op), 结果: op.result || "尚无结果"};
    return {工具: p.name || "未知工具", 参数: p.arguments || {}};
  }
  if (type === "approval.required") return {工具: p.tool || "未知工具", 原因: p.reason || "此操作需要你的决定。"};
  if (type === "approval.resolved") return {工具: p.tool || "未知工具", 决定: p.auto ? "agent 模式自动批准" : p.approved ? "已允许" : "已拒绝"};
  if (type === "run.interrupted" || type === "run.failed") return {原因: p.error || "查看详情了解原因。"};
  return {类型: event.type, 时间: time(event.ts) || "—"};
}
function renderDetailContent(chapter, work, events) {
  const content = $("#detail-content");
  const selection = state.detail;
  if (selection.target?.type === "block") {
    const block = work.blocks.find((candidate) => candidate.id === selection.target.blockId);
    if (block) {
      content.replaceChildren(factsNode({说明: block.text}), ...block.events.map(rawEventNode));
      return;
    }
  }
  if (selection.eventSeq != null) {
    const event = events.find((candidate) => (candidate.session_seq ?? candidate.seq) === selection.eventSeq);
    if (event) {
      content.replaceChildren(factsNode(eventFacts(event, work)), ...events.filter((candidate) => {
        const p = candidate.payload || {};
        return candidate !== event && eventType(candidate.type).startsWith("tool.") && p.call_id === event.payload?.call_id && candidate.run_id === event.run_id;
      }).map(rawEventNode), rawEventNode(event));
      return;
    }
  }
  if (selection.runId) {
    const run = chapter.runs.find((candidate) => runId(candidate) === selection.runId);
    if (run) {
      content.replaceChildren(factsNode({
        Run: {id: runId(run), status: run.status, continuation_of: run.continuation_of || null, steps: run.steps || 0},
        说明: "时间线已筛选为这一次执行；点击时间线节点查看具体事件。",
      }));
      return;
    }
  }
  content.replaceChildren(factsNode({
    Session: state.session,
    Runs: chapter.runs.map((r) => ({id: runId(r), status: r.status, continuation_of: r.continuation_of})),
    恢复证据: work.checkpoint ? "已记录源码 checkpoint；Continue 时仍需校验" : "未加载到可用 checkpoint，不代表可以恢复",
  }));
}
function renderDetail() {
  const chapter = chapters(currentRuns()).find((g) => g.id === state.detail.chapterId) || null;
  const timelineEl = $("#timeline");
  const filters = $("#detail-filters");
  const content = $("#detail-content");
  const exportButton = $("#export-run");
  if (!chapter) {
    timelineEl.replaceChildren(el("p", "", "展开一个章节查看时间线。"));
    filters.hidden = true;
    content.replaceChildren();
    exportButton.hidden = true;
    return;
  }
  filters.hidden = false;
  exportButton.hidden = false;
  renderFilters();
  const work = projectCached(chapter);
  const allEvents = mergeEvents([], chapter.runs.flatMap((run) => feed.events.get(runId(run)) || [])).filter((event) => !event.partial);
  const scoped = state.detail.runId ? allEvents.filter((event) => event.run_id === state.detail.runId) : allEvents;
  const nodes = timeline(scoped, chapter.runs).filter((node) => state.detail.filter === "all" || categoryOf(node) === state.detail.filter);
  reconcile(timelineEl, nodes, (node) => `${node.kind}:${node.seq}`, (node) => JSON.stringify([node.label, node.status, node.ts, state.detail.eventSeq === node.seq]), (node) => {
    const item = el("button", "tl-node");
    item.type = "button";
    item.dataset.kind = node.kind;
    item.dataset.status = node.status;
    item.dataset.seq = String(node.seq);
    item.dataset.focus = String(node.seq);
    if (state.detail.eventSeq === node.seq) item.setAttribute("aria-current", "true");
    item.append(el("span", "tl-label", node.label), el("time", "", time(node.ts)));
    item.onclick = () => { state.detail.eventSeq = node.seq; renderDetail(); };
    return item;
  });
  if (!nodes.length) timelineEl.append(el("p", "", "没有匹配此筛选的事件。"));
  renderDetailContent(chapter, work, allEvents);
}

function runDurationSeconds(run) {
  if (typeof run?.duration_ms === "number" && run.duration_ms >= 0) return run.duration_ms / 1000;
  if (run?.completed_at && run?.started_at) return Math.max(0, run.completed_at - run.started_at);
  if (run?.last_activity_at && run?.started_at) return Math.max(0, run.last_activity_at - run.started_at);
  return null;
}
function lastRequestChars() {
  const chapter = chapters(currentRuns()).find((g) => g.id === state.detail.chapterId);
  if (!chapter) return null;
  let chars = null;
  for (const run of chapter.runs) {
    for (const event of feed.events.get(runId(run)) || []) {
      if (!event.partial && eventType(event.type) === "model.request" && typeof event.payload?.request_chars === "number") chars = event.payload.request_chars;
    }
  }
  return chars;
}
function contextMeterNode(pct) {
  const bar = el("div", "usage-meter");
  bar.style.setProperty("--fill", `${pct}%`);
  bar.append(el("div", "usage-meter-fill"));
  return bar;
}
function renderUsage() {
  const host = $("#usage-content");
  const runs = currentRuns();
  if (!state.session || !runs.length) {
    host.replaceChildren(el("p", "", "选择一个有执行记录的工作，这里会显示它的模型用量。"));
    return;
  }
  const latest = runs.at(-1);
  const detail = state.usageData;
  const fetched = state.usageRunId === runId(latest);
  const totals = {input: 0, output: 0, counted: 0};
  for (const run of runs) {
    const usage = run.usage;
    if (typeof usage?.input_tokens !== "number" || typeof usage?.output_tokens !== "number") continue;
    totals.input += usage.input_tokens;
    totals.output += usage.output_tokens;
    totals.counted++;
  }
  const rows = el("div");
  rows.append(el("h4", "", `最新 run#${state.runNumbers.get(runId(latest)) || "?"}`));
  if (detail) {
    const seconds = runDurationSeconds(detail);
    rows.append(el("p", "", `输入 ${detail.usage?.input_tokens ?? "—"} tokens · 输出 ${detail.usage?.output_tokens ?? "—"} tokens`));
    rows.append(el("p", "", `${detail.steps ?? "—"} 步 · ${detail.tool_calls ?? "—"} 次工具调用${seconds == null ? "" : ` · 时长 ${duration(seconds)}`}`));
  } else {
    rows.append(el("p", "", fetched ? "暂无 run 详情。" : "正在读取 run 详情…"));
  }
  rows.append(el("h4", "", "会话累计"));
  rows.append(el("p", "", totals.counted
    ? `${totals.counted} 次 run · 输入 ${totals.input} tokens · 输出 ${totals.output} tokens`
    : "暂无 token 用量数据。"));
  const pct = contextPct(lastRequestChars(), contextLimit());
  if (pct !== null) {
    rows.append(el("h4", "", "上下文"));
    const line = el("p", "", `已用约 ${pct}% 的上下文窗口（${contextLimit()} 字符）`);
    if (pct > 80) line.classList.add("warning");
    const bar = contextMeterNode(pct);
    if (pct > 80) bar.classList.add("warning");
    rows.append(line, bar);
  }
  host.replaceChildren(rows);
}
async function syncUsage() {
  const owner = state.session;
  const latest = currentRuns().at(-1);
  if (!owner || !latest) return;
  const id = runId(latest);
  if (state.usageRunId === id) return;
  state.usageRunId = id;
  try {
    const data = await request(`/api/runs/${id}`);
    if (state.session === owner && state.usageRunId === id) state.usageData = data;
  } catch {
    if (state.session === owner && state.usageRunId === id) state.usageData = null;
  } finally {
    if (state.session === owner && state.usageRunId === id) renderUsage();
  }
}

// ---- composer ----
const modeLabels = {auto: "Auto", plan: "Plan", agent: "Agent"};
const modeOrder = ["auto", "plan", "agent"];
const modeKey = () => `eventide.mode.${state.session || "new"}`;
const currentMode = () => {
  const value = localStorage.getItem(modeKey());
  return modeOrder.includes(value) ? value : "auto";
};
function renderModeButton() { $("#mode-label").textContent = modeLabels[currentMode()]; }
function renderChips() {
  const host = $("#attachment-chips");
  const chips = state.attachments.get(state.session) || [];
  host.hidden = !chips.length;
  reconcile(host, chips, (chip) => chip.attachment_id, (chip) => chip.name, (chip) => {
    const node = el("span", "attachment-chip");
    node.dataset.attachmentId = chip.attachment_id;
    node.append(el("span", "", chip.name));
    const remove = button("", () => void removeAttachment(chip), "chip-remove");
    remove.setAttribute("aria-label", `移除附件 ${chip.name}`);
    remove.append(icon("close"));
    node.append(remove);
    return node;
  });
}
async function loadPendingAttachments(owner) {
  if (!owner || state.attachmentsUnavailable) return;
  try {
    const records = await request(`/api/sessions/${owner}/attachments`);
    state.attachments.set(owner, Array.isArray(records) ? records : []);
    if (state.session === owner) renderChips();
  } catch (error) {
    if (/404|405|not found|method not allowed/i.test(error.message || "")) {
      state.attachmentsUnavailable = true;
      $("#attach-button").disabled = true;
    }
  }
}
async function removeAttachment(chip) {
  const owner = state.session;
  if (!owner) return;
  try {
    await request(`/api/sessions/${owner}/attachments/${chip.attachment_id}`, {method: "DELETE"});
    const remaining = (state.attachments.get(owner) || []).filter((item) => item.attachment_id !== chip.attachment_id);
    state.attachments.set(owner, remaining);
    renderChips();
  } catch (error) { toastError(error, () => void removeAttachment(chip)); }
}
function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error(`无法读取文件 ${file.name}`));
    reader.readAsDataURL(file);
  });
}
async function uploadAttachments(files) {
  if (!files.length || state.attachmentsUnavailable) return;
  for (const file of files) {
    try {
      const content = await fileBase64(file);
      let owner = state.session;
      if (!owner) owner = await createSession(state.workspace);
      const result = await request(`/api/sessions/${owner}/attachments`, {method: "POST", body: JSON.stringify({name: file.name, media_type: file.type || null, content_base64: content})});
      const chips = state.attachments.get(owner) || [];
      chips.push({attachment_id: result.attachment_id, name: result.name || file.name});
      state.attachments.set(owner, chips);
      renderChips();
    } catch (error) {
      // The contract degrades rather than pretending: once the endpoint is missing
      // or failing, the affordance goes away and the reason is announced once.
      state.attachmentsUnavailable = true;
      const attach = $("#attach-button");
      attach.disabled = true;
      attach.title = "后端接口未实现（见 docs/api-contract.md）";
      if (!state.attachmentsToasted) {
        state.attachmentsToasted = true;
        showToast("error", `附件不可用：${error.message || error}`, {retry: () => {
          state.attachmentsUnavailable = false;
          attach.disabled = false;
          attach.title = "";
        }});
      }
      break;
    }
  }
}
function renderContextMeter() {
  const meter = $("#context-meter");
  const pct = contextPct(lastRequestChars(), contextLimit());
  meter.hidden = pct === null;
  if (pct === null) return;
  meter.style.setProperty("--fill", `${pct}%`);
  meter.classList.toggle("warning", pct > 80);
  meter.textContent = `上下文 ${pct}%`;
}
function renderRunModels() {
  const select = $("#run-model");
  const selected = select.value;
  const models = [...new Set([state.providerDefaultModel, ...state.availableModels].filter(Boolean))];
  select.replaceChildren();
  const fallback = document.createElement("option");
  fallback.value = "";
  fallback.textContent = state.providerDefaultModel ? `默认 · ${state.providerDefaultModel}` : "默认模型";
  select.append(fallback);
  for (const model of models) {
    if (model === state.providerDefaultModel) continue;
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    select.append(option);
  }
  select.value = models.includes(selected) ? selected : "";
}
async function loadRunModels(providerConfig = null) {
  if (providerConfig?.model) state.providerDefaultModel = providerConfig.model;
  try {
    const catalog = await request("/api/models");
    state.availableModels = Array.isArray(catalog.models) ? catalog.models : [];
  } catch { state.availableModels = []; }
  renderRunModels();
}

async function startRun(event) {
  event.preventDefault();
  const prompt = $("#prompt").value.trim();
  if (!prompt || $("#run-button").disabled) return;
  if (!config?.isConfigured()) {
    try { config?.showConfig("请先配置模型连接，草稿会保留。"); } catch { /* dialog unavailable */ }
    return;
  }
  const workspace = state.workspace;
  let owner = state.session;
  const lock = owner || `new:${workspace}`;
  state.busy.add(lock); state.errors.delete(lock); render();
  try {
    if (!owner) {
      // Submitting the first prompt of a brand-new work is also a navigation intent.
      claimView();
      owner = await createSession(workspace);
    }
    const body = {prompt, mode: currentMode(), attachment_ids: (state.attachments.get(owner) || []).map((chip) => chip.attachment_id)};
    const selectedModel = $("#run-model").value;
    if (selectedModel) body.model = selectedModel;
    const accepted = await request(`/api/sessions/${owner}/runs`, {method: "POST", body: JSON.stringify(body)});
    state.drafts.delete(lock); state.drafts.delete(owner);
    state.attachments.delete(owner);
    if (state.session === owner) { $("#prompt").value = ""; resetPromptHeight(); }
    renderChips();
    feed.watch(accepted.run_id, owner);
    await refreshSession(owner);
  } catch (error) { report(owner || lock, error); }
  finally { state.busy.delete(lock); scheduleRender(); }
}

async function continueSession() {
  const owner = state.session;
  const previous = sessionRecord(owner)?.latest_run;
  if (!owner || previous?.status !== "interrupted" || $("#continue-button").disabled) return;
  if (!config?.isConfigured()) {
    try { config?.showConfig("请先配置模型连接。"); } catch { /* dialog unavailable */ }
    return;
  }
  state.busy.add(owner); state.errors.delete(owner); render();
  try {
    const accepted = await request(`/api/sessions/${owner}/continue`, {method: "POST"});
    feed.watch(accepted.run_id, owner);
    state.notices.set(owner, "Continue 已接受；恢复会在后台继续，离开页面也不会中断。");
    await refreshSession(owner);
  } catch (error) {
    toastError(error, () => void continueSession());
    await refreshSession(owner).catch(() => {});
  } finally { state.busy.delete(owner); scheduleRender(); }
}

async function abandonInterruption() {
  const owner = state.session;
  if (!owner || sessionRecord(owner)?.status !== "parked" || $("#abandon-button").disabled) return;
  if (!confirm("放弃本次恢复吗？历史会保留，结果未知的操作不会被当作成功或重新执行。")) return;
  state.busy.add(owner); state.errors.delete(owner); render();
  try {
    await request(`/api/sessions/${owner}/abandon`, {method: "POST"});
    state.notices.set(owner, "已放弃本次恢复；历史仍然保留，现在可以提交新的工作。");
    await refreshSession(owner);
  } catch (error) { toastError(error, () => void abandonInterruption()); }
  finally { state.busy.delete(owner); scheduleRender(); }
}

async function cancelCurrentRun() {
  const owner = state.session;
  const run = sessionRecord(owner)?.latest_run;
  if (!owner || terminal(run) || $("#cancel-run").disabled) return;
  const lock = `cancel:${runId(run)}`;
  state.busy.add(lock); state.errors.delete(owner); render();
  try {
    await request(`/api/runs/${runId(run)}/cancel`, {method: "POST"});
    state.notices.set(owner, "执行已停止，工作记录已停驻。");
    await refreshSession(owner);
  } catch (error) { toastError(error, () => void cancelCurrentRun()); }
  finally { state.busy.delete(lock); scheduleRender(); }
}

// ---- chrome: pill, theme, sidebar and panel toggles ----
function renderPill(now = Date.now() / 1000) {
  const pill = $("#session-pill");
  const session = sessionRecord();
  const run = session?.latest_run;
  const active = run && !terminal(run) ? run : null;
  const reconnecting = state.reconnecting.has(state.session) || state.reconnecting.has("poll");
  pill.dataset.status = session?.status || "idle";
  pill.classList.toggle("reconnecting", reconnecting);
  let text;
  if (reconnecting) {
    text = "重连中";
  } else if (active && session) {
    const activity = runActivity(active, feed.events.get(runId(active)) || [], now);
    const plan = planGroups(session.task_state);
    const activeTask = plan.activeId && plan.total ? plan.open.find((task) => task.id === plan.activeId) : null;
    const doing = session.status === "running" && activeTask ? ` · 正在做：${short(activeTask.content, 30)}` : "";
    text = `${labels[session.status] || session.status}${activity.steps ? ` · 第 ${activity.steps} 步` : ""}${doing} · 已运行 ${duration(activity.elapsedSeconds)}`;
  } else {
    text = session ? (labels[session.status] || session.status) : "尚未开始";
  }
  if (pill.textContent !== text) pill.textContent = text;
}
function renderSubtitle(session) {
  const subtitle = $("#session-subtitle");
  let text = "";
  if (session?.status === "parked") text = parkSummary(session?.latest_run) || "";
  else if (session?.status === "completed") text = "本次执行已结束，可以继续追加工作。";
  else if (!session) text = state.workspace ? "选择左侧的工作，或直接描述一个目标。" : "先在左侧添加一个工作区。";
  subtitle.textContent = text;
  subtitle.hidden = !text;
}
function renderActions(session) {
  const owner = draftKey();
  const workspaceBusy = (state.sessions.get(state.workspace) || []).some((s) => ["running", "waiting_for_user"].includes(s.status)) ||
    [...state.busy].some((id) => id === `new:${state.workspace}` || (sessionRecord(id)?.workspace_id === state.workspace));
  const parked = session?.status === "parked";
  $("#recovery").hidden = !parked;
  $("#recovery-reason").textContent = parked ? parkSummary(session?.latest_run) || "执行被中断；可以尝试继续。" : "";
  $("#prompt-form").hidden = parked;
  $("#continue-button").disabled = workspaceBusy;
  $("#abandon-button").disabled = workspaceBusy;
  $("#continue-button").textContent = state.busy.has(owner) ? "正在接续…" : "尝试 Continue";
  $("#run-button").disabled = !state.workspace || workspaceBusy;
  const activeRun = session?.latest_run && !terminal(session.latest_run) ? session.latest_run : null;
  $("#cancel-run").hidden = !activeRun;
  $("#cancel-run").disabled = activeRun ? state.busy.has(`cancel:${runId(activeRun)}`) : true;
  $("#cancel-run").title = $("#cancel-run").disabled && activeRun ? "正在停止…" : "停止运行";
  $("#prompt").disabled = workspaceBusy;
  $("#action-message").hidden = !state.errors.get(owner);
  $("#action-message").textContent = state.errors.get(owner) || "";
}

function syncDrawerBackdrop() { $("#drawer-backdrop").hidden = !$("#sidebar").classList.contains("open"); }

function renderCapsules(session) {
  const plan = planGroups(session?.task_state);
  $("#capsule-plan").textContent = plan.total ? `计划 ${plan.counts.completed}/${plan.total}` : "计划";
  $("#capsule-tools").textContent = lastToolTotals?.total ? `工具 ${lastToolTotals.total}` : "工具";
}

function render() {
  state.runNumbers = new Map(currentRuns().map((run, index) => [runId(run), index + 1]));
  const session = sessionRecord();
  renderNavigation();
  $("#session-title").textContent = session ? title(session) : "从一项工作开始";
  renderSubtitle(session);
  $("#notice").hidden = !state.notices.get(state.session);
  $("#notice").textContent = state.notices.get(state.session) || "";
  renderPill();
  renderApprovals(session);
  renderPlan(session);
  renderHistory(currentRuns());
  renderTools();
  renderDetail();
  renderUsage();
  renderCapsules(session);
  void syncUsage();
  renderChips();
  renderModeButton();
  renderContextMeter();
  renderActions(session);
}

function initChrome() {
  const root = document.documentElement;
  root.dataset.theme = localStorage.getItem("eventide.theme") ||
    (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  $("#theme-toggle").onclick = () => {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem("eventide.theme", next);
  };
  root.dataset.sidebarCollapsed = localStorage.getItem("eventide.sidebar") === "collapsed" ? "true" : "false";
  $("#collapse-sidebar").onclick = () => {
    const collapsed = root.dataset.sidebarCollapsed !== "true";
    root.dataset.sidebarCollapsed = String(collapsed);
    localStorage.setItem("eventide.sidebar", collapsed ? "collapsed" : "open");
  };
  $("#drawer-backdrop").onclick = () => {
    $("#sidebar").classList.remove("open");
    syncDrawerBackdrop();
  };
  $("#open-sidebar").onclick = () => {
    if (matchMedia("(max-width:760px)").matches) {
      $("#sidebar").classList.toggle("open");
      syncDrawerBackdrop();
    } else if (root.dataset.sidebarCollapsed === "true") {
      root.dataset.sidebarCollapsed = "false";
      localStorage.setItem("eventide.sidebar", "open");
    }
  };
}

// ---- capabilities ----
function loadCapabilities(workspaceId) {
  if (!state.capabilities.has(workspaceId)) {
    const job = request(`/api/workspaces/${workspaceId}/capabilities`);
    state.capabilities.set(workspaceId, job);
    job.catch(() => state.capabilities.delete(workspaceId));
  }
  return state.capabilities.get(workspaceId);
}
function capabilitiesNode(data) {
  const root = el("div");
  const skills = data.skills || [];
  const mcp = data.mcp || [];
  const paths = data.paths || {};
  const notes = data.notes || [];
  if (!skills.length && !mcp.length && !notes.length) root.append(el("p", "", "未发现可用的技能或 MCP 服务。"));
  if (skills.length) {
    const section = el("div");
    section.append(el("h4", "", `技能 (${skills.length})`));
    for (const skill of skills) {
      const row = el("div");
      row.append(el("strong", "", skill.name || "未命名技能"));
      if (skill.description) row.append(el("p", "", skill.description));
      section.append(row);
    }
    root.append(section);
  }
  if (mcp.length) {
    const section = el("div");
    section.append(el("h4", "", `MCP 服务 (${mcp.length})`));
    for (const server of mcp) section.append(el("div", "", `${server.name || "未知服务"} · ${server.transport || "unknown"}`));
    root.append(section);
  }
  const pathEntries = Object.entries(paths);
  if (pathEntries.length) {
    const section = el("div");
    section.append(el("h4", "", "路径（点击复制）"));
    for (const [, value] of pathEntries) {
      const copy = button(String(value), async () => {
        try {
          await navigator.clipboard.writeText(String(value));
          copy.textContent = "已复制";
          setTimeout(() => { if (copy.isConnected) copy.textContent = String(value); }, 1500);
        } catch { /* clipboard unavailable */ }
      });
      copy.title = "点击复制";
      section.append(copy);
    }
    root.append(section);
  }
  for (const note of notes) root.append(el("p", "", note));
  return root;
}
async function renderCapabilities() {
  const host = $("#capabilities-content");
  const workspaceId = state.workspace;
  if (!workspaceId) {
    host.replaceChildren(el("p", "", "先添加一个工作区，再查看它的能力。"));
    return;
  }
  host.replaceChildren(el("p", "", "正在读取工作区能力…"));
  try {
    const data = await loadCapabilities(workspaceId);
    if (state.workspace !== workspaceId) return;
    host.replaceChildren(capabilitiesNode(data));
  } catch (error) {
    if (state.workspace !== workspaceId) return;
    host.replaceChildren(el("p", "", `无法读取能力：${error.message || error}`));
  }
}

// ---- wiring ----
$("#workspace-switcher-button").onclick = () => {
  const menu = $("#workspace-menu");
  if (menu.hidden) {
    renderWorkspaceMenu();
    menu.hidden = false;
    $("#workspace-switcher-button").setAttribute("aria-expanded", "true");
  } else closeWorkspaceMenu();
};
document.addEventListener("click", (event) => {
  // 点击可能同步重建了目标节点（时间线/工具行），冒泡到此处时 target 已游离；
  // 游离目标不能当作"点在面板外"处理，否则面板会被误关。
  if (!(event.target instanceof Element) || !event.target.isConnected) return;
  const menu = $("#workspace-menu");
  if (!menu.hidden && !menu.contains(event.target) && !$("#workspace-switcher-button").contains(event.target)) closeWorkspaceMenu();
  const modeMenu = $("#mode-menu");
  if (!modeMenu.hidden && !modeMenu.contains(event.target) && !$("#mode-select").contains(event.target)) closeModeMenu();
  if (openCapsule && !$("#floating-panel").contains(event.target) && !$("#panel-capsules").contains(event.target)
    && !(event.target.closest?.("[data-panel-open]"))) closePanel({restoreFocus: false});
});
$("#manage-workspace").onclick = () => void removeCurrentWorkspace();
$("#add-workspace").onclick = () => { $("#workspace-message").textContent = ""; $("#workspace-dialog").showModal(); $("#workspace-input").focus(); };
$("#close-workspace-dialog").onclick = () => $("#workspace-dialog").close();
$("#workspace-form").onsubmit = addWorkspace;
$("#toggle-archived").onclick = async () => {
  if (!state.workspace) return;
  if (state.showArchived.has(state.workspace)) state.showArchived.delete(state.workspace);
  else state.showArchived.add(state.workspace);
  await refreshWorkspace(state.workspace).catch((error) => toastError(error, () => void refreshWorkspace(state.workspace)));
};
$("#close-session-dialog").onclick = () => $("#session-dialog").close();
$("#session-form").onsubmit = saveSessionTitle;
$("#archive-session").onclick = () => void toggleSessionArchive();
$("#delete-session").onclick = () => void deleteEmptySession();
$("#session-dialog").addEventListener("close", () => { state.sessionEditing = null; });
async function createNewSession() {
  const workspace = state.workspace, lock = `new:${workspace}`;
  if (!workspace || state.busy.has(lock)) return;
  state.busy.add(lock);
  claimView();
  render();
  try { await createSession(workspace); } catch (error) { toastError(error, () => void createNewSession()); }
  finally { state.busy.delete(lock); scheduleRender(); }
}
$("#new-session").onclick = () => void createNewSession();
$("#prompt-form").onsubmit = startRun;
$("#prompt").addEventListener("input", () => {
  state.drafts.set(draftKey(), $("#prompt").value);
  resetPromptHeight();
});
$("#prompt").addEventListener("keydown", (event) => {
  if (event.isComposing) return;
  if (event.key === "Enter" && (event.ctrlKey || !event.shiftKey)) {
    event.preventDefault();
    $("#prompt-form").requestSubmit();
  }
});
$("#mode-select").onclick = () => {
  const menu = $("#mode-menu");
  menu.hidden = !menu.hidden;
  $("#mode-select").setAttribute("aria-expanded", String(!menu.hidden));
  if (!menu.hidden) { renderModeMenu(); positionModeMenu(); }
};
function renderModeMenu() {
  for (const option of $("#mode-menu").querySelectorAll(".mode-option")) option.setAttribute("aria-checked", String(option.dataset.mode === currentMode()));
}
function closeModeMenu() {
  $("#mode-menu").hidden = true;
  $("#mode-select").setAttribute("aria-expanded", "false");
}
// fixed 定位按按钮实测位置计算，菜单向上展开且不再被 .action-area 的 overflow 裁剪。
function positionModeMenu() {
  const menu = $("#mode-menu"), button = $("#mode-select");
  const rect = button.getBoundingClientRect();
  menu.style.top = `${Math.max(8, rect.top - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8)}px`;
}
for (const option of $("#mode-menu").querySelectorAll(".mode-option")) {
  option.onclick = () => {
    localStorage.setItem(modeKey(), option.dataset.mode);
    closeModeMenu();
    renderModeButton();
  };
}
$("#attach-button").onclick = () => { if (!$("#attach-button").disabled) $("#file-input").click(); };
$("#file-input").onchange = async () => {
  await uploadAttachments([...$("#file-input").files]);
  $("#file-input").value = "";
};
$("#model-button").onclick = () => { try { config?.showConfig(); } catch { /* dialog unavailable */ } };
$("#continue-button").onclick = () => void continueSession();
$("#abandon-button").onclick = () => void abandonInterruption();
$("#cancel-run").onclick = () => void cancelCurrentRun();
$("#load-older").onclick = () => void loadOlderRuns();
$("#interruption-details").onclick = () => {
  const last = chapters(currentRuns()).at(-1);
  if (last) openDetailAtRun(last.id, runId(last.runs.at(-1)));
  else openPanel("detail");
};
$("#export-run").onclick = () => {
  const chapter = chapters(currentRuns()).find((g) => g.id === state.detail.chapterId);
  const run = chapter?.runs.at(-1);
  if (run) window.open(`/api/runs/${runId(run)}/export`);
};
$("#capabilities-button").onclick = () => { $("#capabilities-dialog").showModal(); void renderCapabilities(); };
$("#close-capabilities").onclick = () => $("#capabilities-dialog").close();
$("#close-panel").onclick = () => closePanel();
for (const name of Object.keys(CAPSULE_TITLES)) $(`#capsule-${name}`).onclick = () => togglePanel(name);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  closeWorkspaceMenu();
  closeModeMenu();
  if (openCapsule) closePanel();
  if (matchMedia("(max-width:760px)").matches) {
    $("#sidebar").classList.remove("open");
    syncDrawerBackdrop();
  }
});

window.addEventListener("resize", () => { if (!$("#mode-menu").hidden) positionModeMenu(); });
window.addEventListener("eventide:provider-config", (event) => void loadRunModels(event.detail));
$(".action-area").addEventListener("scroll", () => closeModeMenu(), {passive: true});

async function bootstrap() {
  try {
    const [ providerConfig, workspaces] = await Promise.all([
      config?.loadProviderConfig().catch((error) => showToast("error", `模型配置状态读取失败：${error.message || error}`, {retry: () => void config?.loadProviderConfig().catch(() => {})})) ?? Promise.resolve(),
      request("/api/workspaces"),
    ]);
    await loadRunModels(providerConfig);
    state.workspaces = workspaces;
    const workspace = workspaces.find((w) => w.workspace_id === state.workspace) || workspaces[0];
    if (workspace) await selectWorkspace(workspace.workspace_id);
    else render();
  } catch (error) {
    toastError(error, () => void bootstrap());
    render();
  }
}

initChrome();
void loadSettings().then(() => { renderContextMeter(); renderUsage(); });
void bootstrap();
// Discover work started elsewhere and reconcile authoritative approval/run state.
let polling = false;
setInterval(async () => {
  if (polling || !state.workspace || document.hidden) return;
  polling = true;
  const owner = state.session;
  try {
    await refreshWorkspace(state.workspace);
    if (owner) await refreshSession(owner);
    if (state.reconnecting.delete("poll")) renderPill();
  } catch {
    if (!state.reconnecting.has("poll")) {
      state.reconnecting.add("poll");
      showToast("error", "暂时无法更新工作状态，正在重连…", {retry: () => void refreshSession(state.session).catch(() => {})});
      renderPill();
    }
  } finally { polling = false; }
}, 2500);
// The light 1s path only moves the pill text (steps/elapsed) so chapter reconciliation
// and panel rebuilds are never triggered by the clock.
setInterval(() => {
  const run = sessionRecord()?.latest_run;
  if (run && !terminal(run) && !document.hidden) renderPill();
}, 1000);
