import {chapters, parkLabel, parkSummary, planGroups, projectWork, runActivity, runId, terminal, labels, short, operationText} from "./projection.js?v=11";
import {EventFeed, request} from "./transport.js?v=11";
import {el, button, reconcile, markdown} from "./view.js?v=11";
import {loadProviderConfig, isConfigured, showConfig} from "./config.js?v=11";

const $ = (selector) => document.querySelector(selector);
const state = {
  workspace: localStorage.getItem("eventide.workspace"), session: null, workspaces: [],
  sessions: new Map(), runs: new Map(), expanded: new Map(), manualExpansion: new Set(), errors: new Map(), notices: new Map(),
  drafts: new Map(), positions: new Map(), busy: new Set(), approvals: new Set(), inspector: null,
  hasOlder: new Map(), loadingOlder: new Set(),
  showArchived: new Set(), sessionEditing: null, planCompletedOpen: false,
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
let inspectorTrigger;
let frame;
function scheduleRender() {
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = null; render(); });
}
const feed = new EventFeed((owner, event) => {
  scheduleRender();
  // task.plan_updated refreshes the authoritative plan from session_status, so the
  // UI never folds plan events itself; other state facts keep their existing cues.
  if (["approval.required", "approval.resolved", "run.started", "run.completed", "run.failed", "run.interrupted", "task.plan_updated", "stream.settled"].includes(event.type)) {
    void refreshSession(owner).catch((error) => report(owner, error));
  }
}, (owner, message) => { state.notices.set(owner, message); scheduleRender(); });

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
  } catch (error) { report(owner, error); }
  finally { state.loadingOlder.delete(owner); scheduleRender(); }
}

async function loadChapter(owner, chapter) {
  if (loadingChapters.has(chapter.id)) return loadingChapters.get(chapter.id);
  const job = Promise.all(chapter.runs.map((run) => terminal(run) ? feed.history(runId(run), owner) : feed.watch(runId(run), owner)))
    .finally(() => { loadingChapters.delete(chapter.id); scheduleRender(); });
  loadingChapters.set(chapter.id, job);
  return job;
}

function rememberView() {
  state.drafts.set(draftKey(), $("#prompt").value);
  if (state.session) state.positions.set(state.session, $("#work-scroll").scrollTop);
}
function closeNav() {
  document.body.classList.remove("nav-mobile-open");
  $("#open-nav").setAttribute("aria-expanded", String(!matchMedia("(max-width:760px)").matches && !document.body.classList.contains("nav-collapsed")));
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
  closeInspector(); closeNav(); render();
  $("#work-scroll").scrollTop = state.positions.get(id) || 0;
  if (id) await refreshSession(id).catch((error) => report(id, error));
}
async function selectWorkspace(id) {
  rememberView();
  const version = ++viewVersion;
  state.workspace = id; state.session = null;
  $("#prompt").value = state.drafts.get(draftKey()) || "";
  localStorage.setItem("eventide.workspace", id);
  closeInspector(); render();
  try {
    await refreshWorkspace(id);
    if (state.workspace !== id || version !== viewVersion) return;
    const sessions = state.sessions.get(id) || [];
    const remembered = localStorage.getItem(`eventide.session.${id}`) || localStorage.getItem("eventide.session");
    await selectSession(sessions.find((s) => s.session_id === remembered)?.session_id || sessions[0]?.session_id || null);
  } catch (error) { report(`new:${id}`, error); }
}

async function createSession(workspace = state.workspace) {
  const version = viewVersion;
  const result = await request("/api/sessions", {method: "POST", body: JSON.stringify({workspace_id: workspace})});
  await refreshSession(result.session_id);
  if (state.workspace === workspace && viewVersion === version) await selectSession(result.session_id);
  return result.session_id;
}

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

function renderNavigation() {
  const workspace = state.workspaces.find((w) => w.workspace_id === state.workspace);
  $("#workspace-name").textContent = workspace?.name || "工作空间";
  $("#workspace-path").textContent = workspace?.path || "";
  $("#workspace-git").textContent = workspace?.git_head ? `${workspace.git_branch || "detached"} · ${workspace.git_head.slice(0, 7)}` : workspace?.git_root ? "尚无可用 Git 提交" : "非 Git 工作区";
  const select = $("#workspace-switcher");
  if (select.options.length !== state.workspaces.length) {
    select.replaceChildren(...state.workspaces.map((w) => { const option = el("option", "", w.name); option.value = w.workspace_id; return option; }));
  }
  select.value = state.workspace || "";
  const sessions = state.sessions.get(state.workspace) || [];
  reconcile($("#session-list"), sessions, (s) => s.session_id, (s) => JSON.stringify([s.title, s.status, s.updated_at, s.archived, s.working_directory, state.session === s.session_id]), (s) => {
    const row = el("div", `session-item${s.archived ? " archived" : ""}`);
    row.setAttribute("aria-current", String(s.session_id === state.session));
    row.oncontextmenu = (event) => openSessionActions(s, event);
    const selectSessionButton = button("", () => void selectSession(s.session_id), "session-select");
    selectSessionButton.setAttribute("aria-current", String(s.session_id === state.session));
    selectSessionButton.append(el("strong", "", title(s)), el("small", `status-${s.status}`, `${s.archived ? "已归档 · " : ""}${labels[s.status] || s.status} · ${time(s.updated_at)}`));
    const more = button("⋯", (event) => openSessionActions(s, event), "session-more");
    more.setAttribute("aria-label", `管理 ${title(s)}`);
    row.append(selectSessionButton, more);
    return row;
  });
  $("#new-session").disabled = !state.workspace || state.busy.has(`new:${state.workspace}`);
  $("#toggle-archived").textContent = state.showArchived.has(state.workspace) ? "隐藏已归档" : "显示已归档";
}

function renderOutcome(runs) {
  const completed = [...runs].reverse().find((r) => r.status === "completed" && r.output);
  reconcile($("#outcome"), completed ? [completed] : [], runId, (r) => r.output, (r) => {
    const section = el("div");
    const heading = el("div", "section-heading");
    const copy = button("复制", async () => {
      try { await navigator.clipboard.writeText(r.output); copy.textContent = "已复制"; }
      catch { copy.textContent = "复制失败，请选择文本复制"; }
    });
    heading.append(el("h2", "", "已有成果"), copy);
    section.append(heading, markdown(r.output)); return section;
  });
}

function renderPlan(session) {
  const plan = session ? planGroups(session.task_state) : null;
  const section = $("#plan");
  section.hidden = !plan || !plan.total;
  if (!plan || !plan.total) { reconcile(section, [], () => "", () => "", () => el("div")); return; }
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
    heading.append(el("h2", "", "任务计划"), el("span", "plan-counts", parts.join(" · ")));
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
      const line = el("div", `plan-evidence-item${entry.failed ? " failed" : ""}`);
      line.textContent = entry.text;
      details.append(line);
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
  } catch (error) { report(owner, error); }
  finally { await refreshSession(owner).catch((error) => report(owner, error)); state.approvals.delete(approvalId); scheduleRender(); }
}

function renderBlock(block, chapterId) {
  const node = el("div", `work-block kind-${block.kind}`);
  node.append(el("h3", "", block.title), el("p", "", block.text));
  if (block.operations.length) {
    const details = el("details", "operations"); details.dataset.disclosure = block.id;
    details.append(el("summary", "", `查看操作 (${block.operations.length})`));
    for (const op of block.operations) {
      const action = button(operationText(op), (event) => openInspector({chapterId, operationId: op.id}, event.currentTarget), `operation status-${op.status}`);
      action.dataset.focus = op.id; details.append(action);
    }
    node.append(details);
  } else if (block.kind === "attention" || block.kind === "approval") {
    // Only facts a user may need to act on keep a dedicated entry point; the whole
    // chapter remains inspectable from the section heading.
    const action = button("查看详情", (event) => openInspector({chapterId, blockId: block.id}, event.currentTarget));
    action.dataset.focus = block.id; node.append(action);
  }
  return node;
}

function renderHistory(runs) {
  const owner = state.session;
  $("#load-older").hidden = !owner || !state.hasOlder.get(owner);
  $("#load-older").disabled = state.loadingOlder.has(owner);
  $("#load-older").textContent = state.loadingOlder.has(owner) ? "正在加载…" : "加载更早记录";
  const groups = chapters(runs);
  const latestResult = [...runs].reverse().find((r) => r.status === "completed" && r.output);
  if (!groups.length) {
    const emptyText = owner ? "还没有执行记录。描述下一步要完成的工作，记录将在这里持续展开。"
      : state.workspace ? "选择左侧的工作，或直接描述一个目标。首次提交时创建工作记录。"
        : "先在左侧添加一个工作区，再描述第一个目标。";
    reconcile($("#narrative"), [owner || "empty"], (id) => id, () => "empty", () => el("p", "empty-work", emptyText));
    return;
  }
  reconcile($("#narrative"), groups, (g) => g.id, (g) => g.id, (g) => {
    const details = el("details", "chapter"); details.dataset.disclosure = g.id;
    details.open = state.expanded.get(g.id) ?? g.id === groups.at(-1).id;
    details.append(el("summary"), el("div", "chapter-body"));
    details.firstElementChild.addEventListener("click", () => state.manualExpansion.add(g.id));
    details.addEventListener("toggle", () => {
      state.expanded.set(g.id, details.open);
      if (details.open) {
        const fresh = chapters(state.runs.get(owner) || []).find((c) => c.id === g.id);
        if (fresh) void loadChapter(owner, fresh).catch((error) => report(owner, error));
      }
    });
    return details;
  });
  for (const chapter of groups) {
    const node = [...$("#narrative").children].find((n) => n.dataset.key === chapter.id);
    const expanded = state.expanded.get(chapter.id);
    if (expanded !== undefined && node.open !== expanded) node.open = expanded;
    const work = projectWork(chapter.runs, feed.events, taskContents());
    const label = work.intent ? short(work.intent, 80) : chapter.id === groups[0].id ? "最初的目标" : "后续工作";
    const summary = node.firstElementChild;
    const summaryText = `${label} · ${labels[chapter.runs.at(-1).status]} · ${time(chapter.runs[0].started_at)}`;
    if (summary.textContent !== summaryText) summary.textContent = summaryText;
    if (!node.open) continue;
    const content = [];
    if (work.intent && work.intent !== label) content.push({id: "intent", kind: "intent", text: work.intent});
    content.push(...work.blocks);
    if (!work.blocks.length) {
      const loaded = chapter.runs.every((r) => feed.loaded.has(runId(r)));
      content.push({id: "empty", kind: "empty", text: terminal(chapter.runs.at(-1)) ? loaded ? "本次执行没有工具活动。" : "正在读取工作记录…" : "正在准备执行，等待新的工作记录。"});
    }
    const olderResults = chapter.runs.filter((r) => r.output && r.status === "completed" && runId(r) !== runId(latestResult));
    content.push(...olderResults.map((r) => ({id: `result:${runId(r)}`, kind: "result", text: r.output})));
    reconcile(node.lastElementChild, content, (b) => b.id, (b) => JSON.stringify(b), (b) => {
      if (b.kind === "intent") return el("p", "intent", b.text);
      if (b.kind === "empty") return el("p", "empty-work", b.text);
      if (b.kind === "result") { const result = el("section"); result.append(el("h3", "", "当时的结果"), markdown(b.text)); return result; }
      return renderBlock(b, chapter.id);
    });
  }
}

function renderActions(session) {
  const owner = draftKey();
  const workspaceBusy = (state.sessions.get(state.workspace) || []).some((s) => ["running", "waiting_for_user"].includes(s.status)) ||
    [...state.busy].some((id) => id === `new:${state.workspace}` || sessionRecord(id)?.workspace_id === state.workspace);
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
  $("#cancel-run").textContent = $("#cancel-run").disabled && activeRun ? "正在停止…" : "停止";
  $("#run-button").textContent = session?.latest_run ? "提交" : "开始";
  $("#composer-hint").textContent = workspaceBusy ? session?.status === "waiting_for_user" ? "请先处理上方审批。" : "工作区正在执行；结束后可提交下一步。" : "Ctrl + Enter 提交 · 延续同一项工作";
  $("#prompt").disabled = workspaceBusy;
  $("#action-message").hidden = !state.errors.get(owner);
  $("#action-message").textContent = state.errors.get(owner) || "";
}

function render() {
  const session = sessionRecord();
  const active = session?.latest_run && !terminal(session.latest_run) ? session.latest_run : null;
  const activity = active ? runActivity(active, feed.events.get(runId(active)) || []) : null;
  const plan = session ? planGroups(session.task_state) : null;
  const activeTask = plan && plan.activeId && plan.total ? plan.open.find((task) => task.id === plan.activeId) : null;
  renderNavigation();
  $("#session-title").textContent = session ? title(session) : "从一项工作开始";
  const working = activeTask && session.status === "running" ? `正在做：${short(activeTask.content, 40)} · ` : "";
  const parkedLabel = parkLabel(session?.latest_run);
  $("#session-status").textContent = activity
    ? `${labels[session.status] || session.status} · ${activity.steps ? `第 ${activity.steps} 步 · ` : ""}${working}已运行 ${duration(activity.elapsedSeconds)} · ${activity.idleSeconds < 5 ? "刚刚有活动" : `${duration(activity.idleSeconds)}前有活动`}`
    : session ? `${labels[session.status] || session.status}${parkedLabel ? ` · ${parkedLabel}` : ""}${session.status === "completed" ? "，可继续追加工作" : ""}` : "一个工作区，一段持续的工作过程。";
  $("#session-status").className = `session-status status-${session?.status || "idle"}`;
  $("#notice").hidden = !state.notices.get(state.session);
  $("#notice").textContent = state.notices.get(state.session) || "";
  $("#session-details").disabled = !state.session;
  renderApprovals(session); renderPlan(session); renderOutcome(currentRuns()); renderHistory(currentRuns()); renderActions(session);
  if (state.inspector) renderInspector();
}

function openInspector(selection, trigger) {
  inspectorTrigger = {node: trigger, key: trigger?.dataset.focus};
  state.inspector = {...selection, owner: state.session, trigger};
  renderInspector();
  if (!$("#inspector").open) $("#inspector").showModal();
}
function closeInspector() { if ($("#inspector").open) $("#inspector").close(); state.inspector = null; }
function renderInspector() {
  const selection = state.inspector;
  if (!selection) return;
  const runs = state.runs.get(selection.owner) || [];
  const group = chapters(runs).find((g) => g.id === selection.chapterId);
  const work = projectWork(group?.runs || runs, feed.events, taskContents(selection.owner));
  const op = work.operations.get(selection.operationId);
  const block = work.blocks.find((b) => b.id === selection.blockId);
  const events = op?.events || block?.events || work.events;
  $("#inspector-title").textContent = op ? op.name : block?.title || "工作详情";
  const facts = op ? {对象: op.arguments, 状态: operationText(op), 结果: op.result || "尚无结果"} : block ? {说明: block.text} : {
    Session: selection.owner, Runs: runs.map((r) => ({id: runId(r), status: r.status, continuation_of: r.continuation_of})),
    恢复证据: work.checkpoint ? "已记录源码 checkpoint；Continue 时仍需校验" : "未加载到可用 checkpoint，不代表可以恢复",
  };
  const container = $("#inspector-content");
  const items = [{id: "facts", facts}, ...events.map((event) => ({id: event.event_id || `${event.run_id}:${event.seq}`, event}))];
  reconcile(container, items, (item) => item.id, (item) => JSON.stringify(item), (item) => {
    if (item.facts) {
      const section = el("div");
      for (const [key, value] of Object.entries(item.facts)) {
        section.append(el("h3", "", key), typeof value === "string" ? el("p", "", value) : el("pre", "", JSON.stringify(value, null, 2)));
      }
      return section;
    }
    const details = el("details", "raw-event"); details.dataset.disclosure = item.id;
    details.append(el("summary", "", `${item.event.type} · ${time(item.event.ts)}`), el("pre", "", JSON.stringify(item.event, null, 2))); return details;
  });
}

async function startRun(event) {
  event.preventDefault();
  const prompt = $("#prompt").value.trim();
  if (!prompt || $("#run-button").disabled) return;
  if (!isConfigured()) { showConfig("请先配置模型连接，草稿会保留。"); return; }
  const workspace = state.workspace;
  let owner = state.session;
  const lock = owner || `new:${workspace}`;
  state.busy.add(lock); state.errors.delete(lock); render();
  try {
    owner ||= await createSession(workspace);
    const accepted = await request(`/api/sessions/${owner}/runs`, {method: "POST", body: JSON.stringify({prompt})});
    state.drafts.delete(lock); state.drafts.delete(owner);
    if (state.session === owner) $("#prompt").value = "";
    feed.watch(accepted.run_id, owner);
    await refreshSession(owner);
  } catch (error) { report(owner || lock, error); }
  finally { state.busy.delete(lock); scheduleRender(); }
}

async function continueSession() {
  const owner = state.session;
  const previous = sessionRecord(owner)?.latest_run;
  if (!owner || previous?.status !== "interrupted" || $("#continue-button").disabled) return;
  if (!isConfigured()) { showConfig("请先配置模型连接。"); return; }
  state.busy.add(owner); state.errors.delete(owner); render();
  try {
    const accepted = await request(`/api/sessions/${owner}/continue`, {method: "POST"});
    feed.watch(accepted.run_id, owner);
    state.notices.set(owner, "Continue 已接受；恢复会在后台继续，离开页面也不会中断。");
    await refreshSession(owner);
  } catch (error) {
    report(owner, error);
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
  } catch (error) { report(owner, error); }
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
  } catch (error) { report(owner, error); }
  finally { state.busy.delete(lock); scheduleRender(); }
}

$("#workspace-switcher").onchange = (event) => void selectWorkspace(event.target.value);
$("#add-workspace").onclick = () => { $("#workspace-message").textContent = ""; $("#workspace-dialog").showModal(); $("#workspace-input").focus(); };
$("#close-workspace-dialog").onclick = () => $("#workspace-dialog").close();
$("#workspace-form").onsubmit = addWorkspace;
$("#toggle-archived").onclick = async () => {
  if (!state.workspace) return;
  if (state.showArchived.has(state.workspace)) state.showArchived.delete(state.workspace);
  else state.showArchived.add(state.workspace);
  await refreshWorkspace(state.workspace).catch((error) => report(draftKey(), error));
};
$("#close-session-dialog").onclick = () => $("#session-dialog").close();
$("#session-form").onsubmit = saveSessionTitle;
$("#archive-session").onclick = () => void toggleSessionArchive();
$("#delete-session").onclick = () => void deleteEmptySession();
$("#session-dialog").addEventListener("close", () => { state.sessionEditing = null; });
$("#new-session").onclick = async () => {
  const workspace = state.workspace, lock = `new:${workspace}`;
  if (state.busy.has(lock)) return;
  state.busy.add(lock); render();
  try { await createSession(workspace); } catch (error) { report(draftKey(), error); }
  finally { state.busy.delete(lock); scheduleRender(); }
};
$("#prompt-form").onsubmit = startRun;
$("#prompt").oninput = () => state.drafts.set(draftKey(), $("#prompt").value);
$("#prompt").onkeydown = (event) => { if (event.ctrlKey && event.key === "Enter") { event.preventDefault(); $("#prompt-form").requestSubmit(); } };
$("#continue-button").onclick = () => void continueSession();
$("#abandon-button").onclick = () => void abandonInterruption();
$("#cancel-run").onclick = () => void cancelCurrentRun();
$("#load-older").onclick = () => void loadOlderRuns();
$("#session-details").onclick = (event) => openInspector({}, event.currentTarget);
$("#interruption-details").onclick = (event) => openInspector({chapterId: chapters(currentRuns()).at(-1)?.id}, event.currentTarget);
$("#close-inspector").onclick = closeInspector;
$("#inspector").addEventListener("close", () => {
  state.inspector = null;
  const trigger = inspectorTrigger?.node?.isConnected ? inspectorTrigger.node : [...document.querySelectorAll("[data-focus]")].find((n) => n.dataset.focus === inspectorTrigger?.key);
  if (trigger) trigger.focus({preventScroll: true});
  inspectorTrigger = null;
});
$("#open-nav").onclick = () => {
  if (matchMedia("(max-width:760px)").matches) document.body.classList.toggle("nav-mobile-open");
  else document.body.classList.toggle("nav-collapsed");
  $("#open-nav").setAttribute("aria-expanded", String(matchMedia("(max-width:760px)").matches ? document.body.classList.contains("nav-mobile-open") : !document.body.classList.contains("nav-collapsed")));
};
$("#close-nav").onclick = () => { document.body.classList.add("nav-collapsed"); closeNav(); $("#open-nav").focus(); };
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeNav(); });
$("#work-scroll").addEventListener("click", closeNav);

async function bootstrap() {
  try {
    const [, workspaces] = await Promise.all([loadProviderConfig(), request("/api/workspaces")]);
    state.workspaces = workspaces;
    const workspace = workspaces.find((w) => w.workspace_id === state.workspace) || workspaces[0];
    if (workspace) await selectWorkspace(workspace.workspace_id);
    else render();
  } catch (error) { report(draftKey(), error); render(); }
}
void bootstrap();
// Discover work started elsewhere and reconcile authoritative approval/run state.
let polling = false;
setInterval(async () => {
  if (polling || !state.workspace || document.hidden) return;
  polling = true;
  const workspace = state.workspace, owner = state.session;
  try {
    await refreshWorkspace(workspace);
    if (owner) await refreshSession(owner);
    if (state.notices.get(owner) === "暂时无法更新工作状态，正在重连…") state.notices.delete(owner);
  } catch { state.notices.set(owner, "暂时无法更新工作状态，正在重连…"); scheduleRender(); }
  finally { polling = false; }
}, 2500);
setInterval(() => {
  const run = sessionRecord()?.latest_run;
  if (run && !terminal(run) && !document.hidden) scheduleRender();
}, 1000);
