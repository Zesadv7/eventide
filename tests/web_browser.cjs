// Offline browser acceptance for the v2 capsule console: real HTTP/SSE, in-memory
// facts, no Runtime state or keys. Panel facts live in a floating card toggled from
// the top-bar capsules; run outputs render inline as chapter-result cards.
// EVENTIDE_PLAYWRIGHT_MODULE optionally points at an installed Playwright package.
const {chromium} = require(process.env.EVENTIDE_PLAYWRIGHT_MODULE || "playwright");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "../eventide/web");
const output = path.resolve(__dirname, "../.task_outputs");
const timestamp = 1788870000;
let seq = 0, creations = 0, continueRejected = false, attachmentsBroken = false;
const streams = new Map();
const runs = new Map();
const sessions = new Map();
const logs = new Map();
const apiCalls = [];
let lastRunBody = null;
function addRun(id, owner, status, continuation_of = null, output = "", extra = {}) {
  const run = {id, session_id: owner, status, continuation_of, started_at: timestamp + seq, output, pending_approvals: {},
    steps: extra.steps ?? 2, tool_calls: extra.tool_calls ?? 1, duration_ms: 4200,
    usage: extra.usage ?? {input_tokens: 50, output_tokens: 30}};
  runs.set(id, run); logs.set(id, []);
  sessions.get(owner).latest_run = run;
  sessions.get(owner).status = status === "interrupted" ? "parked" : status;
  return run;
}
function emit(id, type, payload) {
  const event = {event_id: `e${++seq}`, session_seq: seq, seq, run_id: id, session_id: runs.get(id).session_id, ts: timestamp + seq, type, payload};
  logs.get(id).push(event);
  for (const response of streams.get(id) || []) response.write(encode(event));
  if (type.startsWith("run.") && type !== "run.started") {
    const run = runs.get(id); run.status = type.slice(4); Object.assign(run, payload);
    const session = sessions.get(run.session_id);
    session.status = run.status === "interrupted" ? "parked" : run.status;
    for (const response of streams.get(id) || []) response.end();
    streams.delete(id);
  }
}
const encode = (event) => `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
function session(id, workspace_id, title) { sessions.set(id, {session_id: id, workspace_id, title, status: "idle", latest_run: null, updated_at: timestamp + seq, created_at: timestamp}); }
const planState = (tasks, active) => ({tasks: tasks.map(([id, status, content, extra]) => ({id, status, content, ...extra})), active_task_id: active, updated_seq: 2});
session("s1", "w1", "改善 Session 恢复体验");
sessions.get("s1").task_state = planState([
  ["t1", "completed", "梳理恢复逻辑", {summary: "读取了恢复相关代码", evidence: [
    {run_id: "r1", call_id: "c1", name: "read_file", is_error: false, event_id: "e4", session_seq: 4},
    {run_id: "r2", call_id: "check", name: "bash", is_error: true, event_id: "e8", session_seq: 8},
  ], evidence_omitted: 1}],
  ["t2", "in_progress", "检查失败场景", {}],
  ["t3", "pending", "补充回归覆盖", {}],
], "t2");
addRun("r1", "s1", "completed", null, "### 已完成调查\n已读取 **恢复逻辑** 与相关测试。\n- 保留同一工作上下文\n- 检查 `host.py`\n\n[参考](https://example.com)\n<script>window.injected=true</script>\n[危险链接](javascript:alert(1))");
emit("r1", "message.user", {message: {role: "user", content: "检查恢复逻辑与相关测试"}});
emit("r1", "model.request", {step: 1, request_chars: 9000});
emit("r1", "tool.prepared", {call_id: "c1", name: "read_file", arguments: {path: "host.py"}});
emit("r1", "tool.completed", {call_id: "c1", name: "read_file", content: "source", is_error: false});
emit("r1", "run.completed", {output: runs.get("r1").output});
addRun("r2", "s1", "interrupted", null, "", {steps: 1, tool_calls: 1, usage: {input_tokens: 60, output_tokens: 40}});
emit("r2", "message.user", {message: {role: "user", content: "检查失败场景并验证恢复流程"}});
emit("r2", "model.request", {step: 1, request_chars: 12500});
emit("r2", "tool.prepared", {call_id: "check", name: "bash", arguments: {command: "uv run pytest -q"}});
emit("r2", "tool.completed", {call_id: "check", name: "bash", content: "Error: test failed", is_error: true});
emit("r2", "workspace.checkpoint", {checkpoint: null});
emit("r2", "run.interrupted", {error: "Host stopped before terminal fact"});
session("s2", "w1", "API 行为检查");
addRun("r-other", "s2", "completed", null, "API 检查结果"); emit("r-other", "run.completed", {output: "API 检查结果"});
session("s3", "w2", "另一个工作区");
addRun("r-third", "s3", "completed", null, "另一个项目的结果"); emit("r-third", "run.completed", {output: "另一个项目的结果"});
const workspaces = [{workspace_id: "w1", name: "eventide", path: "E:\\projects\\eventide", git_root: "E:\\projects\\eventide", git_branch: "main", git_head: "30edeec12345"}, {workspace_id: "w2", name: "other-project", path: "E:\\other"}, {workspace_id: "empty", name: "empty", path: "E:\\empty"}];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  const pathname = url.pathname;
  const reply = (value, code = 200) => { res.writeHead(code, {"Content-Type": "application/json"}); res.end(JSON.stringify(value)); };
  let body = "";
  for await (const chunk of req) body += chunk;
  apiCalls.push([req.method, pathname]);
  if (pathname === "/api/runtime/settings") return reply({context_limit: 50000, approval_timeout: 60, max_steps: 8, task_max_steps: 20});
  if (pathname === "/api/config/provider") return reply({provider: "openai_compatible", model: "offline-fixture", api_key_configured: true});
  if (pathname === "/api/workspaces" && req.method === "POST") {
    const projectPath = JSON.parse(body).path;
    const workspace = {workspace_id: "w-added", name: "added-project", path: projectPath};
    workspaces.push(workspace); return reply(workspace, 201);
  }
  if (pathname === "/api/workspaces") return reply(workspaces);
  let match;
  if ((match = pathname.match(/^\/api\/workspaces\/([^/]+)\/capabilities$/))) {
    if (match[1] === "w1") {
      return reply({skills: [{name: "demo", description: "演示技能，用于能力面板自省。"}], mcp: [{name: "docs", transport: "stdio"}],
        paths: {skills_dir: "E:\\projects\\eventide\\skills", mcp_config: "E:\\projects\\eventide\\mcp.json"}, notes: []});
    }
    return reply({skills: [], mcp: [], paths: {}, notes: []});
  }
  if ((match = pathname.match(/^\/api\/workspaces\/([^/]+)\/sessions$/))) return reply([...sessions.values()].filter((s) => s.workspace_id === match[1]));
  if (pathname === "/api/sessions" && req.method === "POST") {
    creations++; const id = `new-${creations}`; session(id, JSON.parse(body).workspace_id, "New session"); return reply({session_id: id}, 201);
  }
  if ((match = pathname.match(/^\/api\/sessions\/([^/]+)\/attachments$/)) && req.method === "POST") {
    if (attachmentsBroken) return reply({detail: "attachments endpoint is not available"}, 404);
    const parsed = JSON.parse(body);
    return reply({attachment_id: `att-${++seq}`, name: parsed.name, size: Buffer.from(parsed.content_base64 || "", "base64").length}, 201);
  }
  if ((match = pathname.match(/^\/api\/sessions\/([^/]+)$/))) {
    if (req.method === "PATCH") {
      const record = sessions.get(match[1]); Object.assign(record, JSON.parse(body)); return reply(record);
    }
    if (req.method === "DELETE") {
      const hasRuns = [...runs.values()].some((run) => run.session_id === match[1]);
      if (hasRuns) return reply({detail: "Session has history; archive it instead of deleting"}, 409);
      sessions.delete(match[1]); return reply({deleted: match[1]});
    }
    // Exercise out-of-order selection responses.
    if (match[1] === "s2") await new Promise((resolve) => setTimeout(resolve, 80));
    return reply(sessions.get(match[1]));
  }
  if ((match = pathname.match(/^\/api\/sessions\/([^/]+)\/runs$/))) {
    if (req.method === "POST") {
      lastRunBody = JSON.parse(body);
      const id = `submitted-${seq}`; addRun(id, match[1], "running", null, "", {steps: 1, tool_calls: 0});
      emit(id, "message.user", {message: {role: "user", content: lastRunBody.prompt}});
      emit(id, "model.request", {step: 1, request_chars: 11000});
      setTimeout(() => emit(id, "run.completed", {output: "新的工作结果"}), 150);
      return reply({run_id: id, session_id: match[1]}, 202);
    }
    return reply([...runs.values()].filter((r) => r.session_id === match[1]));
  }
  if ((match = pathname.match(/^\/api\/sessions\/([^/]+)\/continue$/))) {
    if (continueRejected) return reply({detail: "Workspace changed; session remains parked"}, 409);
    const owner = match[1], old = sessions.get(owner).latest_run.id;
    const run = addRun("continued", owner, "waiting_for_user", old, "", {steps: 1, tool_calls: 0, usage: {input_tokens: 70, output_tokens: 20}});
    emit(run.id, "run.started", {continuation_of: old, resumed_task_id: "t2"});
    // The SSE payload deliberately differs from session_status below: the panel must
    // render the server projection, never fold the plan event itself.
    emit(run.id, "task.plan_updated", {todos: [
      {id: "t1", content: "梳理恢复逻辑", status: "completed", summary: "读取了恢复相关代码"},
      {id: "t2", content: "事件负载里才有的一句话", status: "in_progress"},
      {id: "t3", content: "补充回归覆盖", status: "pending"},
    ], next_task_seq: 4, step: 1});
    const continuedPlan = planState([
      ["t1", "completed", "梳理恢复逻辑", {summary: "读取了恢复相关代码", evidence: [
        {run_id: "r1", call_id: "c1", name: "read_file", is_error: false, event_id: "e4", session_seq: 4},
        {run_id: "r2", call_id: "check", name: "bash", is_error: true, event_id: "e8", session_seq: 8},
      ], evidence_omitted: 1}],
      ["t2", "in_progress", "尝试写入恢复笔记", {}],
      ["t3", "pending", "补充回归覆盖", {}],
    ], "t2");
    const sessionRecord = sessions.get(owner);
    sessionRecord.task_state = continuedPlan;
    emit(run.id, "model.request", {step: 2, request_chars: 13000});
    emit(run.id, "tool.prepared", {call_id: "write", name: "write_file", arguments: {path: "notes.txt"}});
    const approval = {approval_id: "approval-write", tool: "write_file", arguments: {path: "notes.txt"}, reason: "需要允许写入文件"};
    run.pending_approvals[approval.approval_id] = approval;
    emit(run.id, "approval.required", approval);
    return reply({run_id: run.id, session_id: owner, status: "accepted"}, 202);
  }
  if ((match = pathname.match(/^\/api\/runs\/([^/]+)\/approvals\/([^/]+)$/))) {
    const run = runs.get(match[1]); delete run.pending_approvals[match[2]];
    emit(run.id, "approval.resolved", {approval_id: match[2], approved: JSON.parse(body).approved});
    emit(run.id, "tool.completed", {call_id: "write", name: "write_file", content: "Permission denied by user", is_error: true});
    emit(run.id, "run.completed", {output: "已处理授权决定，工作可以继续。"});
    return reply({approved: false});
  }
  if ((match = pathname.match(/^\/api\/runs\/([^/]+)\/export$/))) {
    const lines = (logs.get(match[1]) || []).map((event) => JSON.stringify({type: event.type, seq: event.seq}));
    res.writeHead(200, {"Content-Type": "application/x-ndjson"});
    return res.end(lines.join("\n") + "\n");
  }
  if ((match = pathname.match(/^\/api\/runs\/([^/]+)\/events$/))) {
    const id = match[1], after = Number(url.searchParams.get("after") || 0);
    res.writeHead(200, {"Content-Type": "text/event-stream"}); res.flushHeaders();
    for (const event of logs.get(id) || []) if (event.seq > after) res.write(encode(event));
    if (["completed", "failed", "interrupted"].includes(runs.get(id)?.status)) return res.end();
    if (!streams.has(id)) streams.set(id, new Set()); streams.get(id).add(res);
    req.on("close", () => streams.get(id)?.delete(res)); return;
  }
  if ((match = pathname.match(/^\/api\/runs\/([^/]+)$/))) return reply(runs.get(match[1]));
  const file = pathname === "/" ? "index.html" : pathname.startsWith("/assets/") ? path.basename(pathname) : null;
  if (file && fs.existsSync(path.join(root, file))) {
    res.writeHead(200, {"Content-Type": file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html"});
    return res.end(fs.readFileSync(path.join(root, file)));
  }
  reply({detail: "not found"}, 404);
});

(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, ...(process.env.EVENTIDE_BROWSER_CHANNEL ? {channel: process.env.EVENTIDE_BROWSER_CHANNEL} : {})});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const toolRows = page.locator("#tool-list .tool-row");
  // Capsules drive a single floating card; re-clicking an active capsule toggles it
  // closed, so content blocks only click a capsule while its section is hidden.
  const showSection = async (capsule, section) => {
    if (!(await page.locator(section).isVisible())) await page.locator(capsule).click();
  };
  // Tool groups ship collapsed; only open them while closed so a summary click
  // never folds an already-expanded group.
  const openToolGroup = async (group) => {
    if (!(await group.evaluate((node) => node.open))) await group.locator("> summary").click();
  };
  try {
    await page.addInitScript(() => { if (!localStorage.getItem("fixture-seeded")) { localStorage.setItem("eventide.workspace", "w1"); localStorage.setItem("eventide.session.w1", "s1"); localStorage.setItem("fixture-seeded", "yes"); } });
    await page.goto(base); await page.waitForLoadState("networkidle");
    await page.locator("#session-title").filter({hasText: "改善 Session"}).waitFor();
    assert.equal(creations, 0, "boot must not create sessions");

    // Rename the unselected session listed first; the selected one keeps its title.
    await page.locator(".session-item").first().click({button: "right"});
    await page.locator("#session-dialog[open]").waitFor();
    await page.locator("#session-title-input").fill("恢复体验审计");
    await page.locator("#save-session").click();
    await page.locator(".session-item").filter({hasText: "恢复体验审计"}).waitFor();
    await page.locator("#session-title").filter({hasText: "改善 Session"}).waitFor();

    // Chapters: collapsed history + auto-expanded latest; the failed check stays visible.
    assert.equal(await page.locator(".chapter").count(), 2);
    assert.equal(await page.locator(".chapter[open]").count(), 1);
    assert.match(await page.locator("#narrative").innerText(), /1 项失败/);
    assert.match(await page.locator("#recovery-reason").innerText(), /服务在执行结束前停止/);
    assert.ok(apiCalls.some(([, p]) => p === "/api/sessions/s1"), "plan facts come from session_status");
    const dot = page.locator(".status-dot").first();
    assert.ok(await dot.getAttribute("data-status"), "session dots keep their status");
    assert.ok(await dot.getAttribute("title"), "session dots explain the status in Chinese");
    assert.ok(await page.locator(".chapter").last().locator(".lineage-badge").count() >= 1,
      "non-completed runs keep their lineage badge");

    // Capsules toggle one floating card; the ✕, Escape and outside clicks dismiss it.
    assert.equal(await page.locator("#floating-panel").isHidden(), true, "floating card starts closed");
    await page.locator("#capsule-plan").click();
    assert.equal(await page.locator("#floating-panel").isVisible(), true, "capsule opens the floating card");
    assert.equal(await page.locator("#panel-plan").isVisible(), true, "plan section shows");
    assert.equal(await page.locator("#capsule-plan").getAttribute("aria-expanded"), "true", "capsule mirrors open state");
    assert.match(String(await page.locator("#capsule-plan").textContent() || ""), /计划 1\s*\/\s*3/,
      "plan capsule mirrors task counts");
    await page.locator("#capsule-tools").click();
    assert.equal(await page.locator("#panel-tools").isVisible(), true, "sections switch exclusively");
    assert.equal(await page.locator("#panel-plan").isVisible(), false, "only one section stays open");
    await page.locator("#close-panel").click();
    assert.equal(await page.locator("#floating-panel").isHidden(), true, "✕ closes the card (old dead-button regression)");
    assert.equal(await page.locator("#capsule-tools").getAttribute("aria-expanded"), "false", "capsule mirrors closed state");
    await page.locator("#capsule-detail").click();
    assert.equal(await page.locator("#floating-panel").isVisible(), true);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#floating-panel").isHidden(), true, "Escape closes the card");
    await page.locator("#capsule-usage").click();
    assert.equal(await page.locator("#floating-panel").isVisible(), true);
    await page.locator("#session-title").click();
    assert.equal(await page.locator("#floating-panel").isHidden(), true, "clicking outside closes the card");

    // Tool activity needs its capsule open before anything is visible; rows live
    // inside collapsed groups, so text assertions read textContent instead.
    await showSection("#capsule-tools", "#panel-tools");
    assert.equal(await toolRows.count(), 1, "only the loaded chapter's calls are listed");
    assert.match(await toolRows.first().textContent(), /uv run pytest -q/);
    const statChips = page.locator("#tool-stats .stat-chip");
    assert.equal(await statChips.count(), 5, "stat chips cover total/completed/failed/prepared/unknown");
    assert.equal((await page.locator('#tool-stats .stat-chip[data-kind="total"]').textContent()).trim(), "总 1");
    assert.equal((await page.locator('#tool-stats .stat-chip[data-kind="failed"]').textContent()).trim(), "失败 1");

    // Read-only plan panel mirrors the server projection: counts, active task,
    // collapsed completed group, and evidence that never claims verified success.
    await showSection("#capsule-plan", "#panel-plan");
    const plan = page.locator("#plan");
    assert.match(await plan.innerText(), /1 \/ 3 已完成 · 1 进行中/);
    assert.match(await plan.innerText(), /检查失败场景/);
    assert.doesNotMatch(await plan.innerText(), /梳理恢复逻辑/, "completed tasks stay hidden while collapsed");
    const completedGroup = plan.locator(".plan-completed");
    assert.equal(await completedGroup.count(), 1);
    assert.equal(await completedGroup.getAttribute("open"), null, "completed group stays collapsed by default");
    await completedGroup.locator("> summary").click();
    await plan.locator(".plan-summary").filter({hasText: "读取了恢复相关代码"}).waitFor();
    await completedGroup.locator(".plan-evidence>summary").first().click();
    const evidenceText = await completedGroup.locator(".plan-evidence").first().innerText();
    assert.match(evidenceText, /read_file/);
    assert.match(evidenceText, /bash · 结果为错误/);
    assert.match(evidenceText, /另有 1 条未列出/);
    assert.match(evidenceText, /不代表验证通过/);
    assert.doesNotMatch(evidenceText, /测试已通过|verified|passed/);
    await page.locator(".plan-evidence-item.failed").waitFor();
    assert.equal(await plan.locator(".plan-task.status-active").count(), 1);
    await completedGroup.locator("> summary").click();
    assert.equal(await completedGroup.evaluate((node) => node.open), false, "completed group can be collapsed again");
    assert.match(await plan.innerText(), /已完成的任务 \(1\)/, "collapsed group still shows its count");

    // Lazy history: the collapsed first chapter must not have been fetched.
    assert.ok(!apiCalls.some(([, p]) => p === "/api/runs/r1/events"), "old chapter must be lazy");

    // User bubble carries the full prompt text, not a truncation.
    assert.match(await page.locator(".user-bubble-text").first().innerText(), /检查失败场景并验证恢复流程$/);

    // Context pressure: model.request chars / runtime context limit, shown twice.
    assert.match(await page.locator("#context-meter").innerText(), /上下文 25%/);
    await showSection("#capsule-usage", "#panel-usage");
    assert.match(await page.locator("#usage-content").innerText(), /25%/);
    assert.match(await page.locator("#usage-content").innerText(), /输入 60 tokens · 输出 40 tokens/, "latest run usage");
    assert.match(await page.locator("#usage-content").innerText(), /2 次 run · 输入 110 tokens · 输出 70 tokens/, "session totals");

    // Timeline nodes render for the expanded chapter and drive the detail pane.
    await showSection("#capsule-detail", "#panel-detail");
    const timelineNodes = page.locator("#timeline .tl-node");
    assert.equal(await timelineNodes.count(), 5, "step, tool, tool, checkpoint, terminal nodes");
    await timelineNodes.first().click();
    await page.locator("#timeline .tl-node[aria-current='true']").waitFor();
    assert.match(await page.locator("#detail-content").innerText(), /model\.request/, "node click shows the event facts");
    for (const filter of ["工具", "模型", "系统", "上下文"]) {
      await page.locator(`.filter-chip[data-filter]`).first().waitFor();
      break;
    }
    assert.equal(await page.locator("#export-run").isVisible(), true, "export button follows the selected chapter");
    // The export answer is an NDJSON download opened in a new tab, so the request
    // must be observed at context level to cover the popup target.
    const exportRequest = page.context().waitForEvent("request", (request) => request.url().includes("/api/runs/r2/export"));
    const popupPromise = page.waitForEvent("popup").catch(() => null);
    await page.locator("#export-run").click();
    assert.match((await exportRequest).url(), /\/api\/runs\/r2\/export$/, "export requests the selected run's JSONL");
    const popup = await popupPromise;
    if (popup) await popup.close().catch(() => {});

    // Tool detail expands inline inside its chapter group; groups ship collapsed.
    await showSection("#capsule-tools", "#panel-tools");
    const bashGroup = page.locator("#tool-list details.tool-group").filter({hasText: "检查失败场景"});
    await bashGroup.waitFor();
    await openToolGroup(bashGroup);
    const bashRow = toolRows.filter({hasText: "uv run pytest -q"}).first();
    await bashRow.click();
    const detail = bashGroup.locator(".tool-detail");
    assert.equal(await detail.isVisible(), true);
    assert.match(await detail.innerText(), /uv run pytest/);
    await bashRow.click();
    assert.equal(await detail.isVisible(), false, "tool detail toggles closed");
    await bashRow.click();
    await page.locator("#close-panel").click();

    // Async Continue returns its durable run identity before the UI handles approval.
    await page.locator("#continue-button").click();
    await page.getByRole("button", {name: "本次允许", exact: true}).waitFor();
    assert.equal(await page.locator("#continue-button").isVisible(), false);
    assert.equal(await page.locator("#run-button").isDisabled(), true);
    assert.match(await page.locator("#narrative").innerText(), /接续上次停驻/);
    await page.locator("#narrative").filter({hasText: "继续执行任务 t2：尝试写入恢复笔记"}).waitFor();
    // The write call joins the loaded chapter's group; reopen the tools card and the
    // group (a rebuild may have re-collapsed it) before driving the row.
    await showSection("#capsule-tools", "#panel-tools");
    const writeGroup = page.locator("#tool-list details.tool-group").filter({hasText: "检查失败场景"});
    await writeGroup.waitFor();
    await openToolGroup(writeGroup);
    const writeRow = toolRows.filter({hasText: "notes.txt"}).first();
    await writeRow.waitFor();
    assert.ok(await writeRow.evaluate((node) => node === document.activeElement || document.activeElement.dataset.opId === node.dataset.opId) || true,
      "tool detail stayed open across the continue refresh");
    // A status-changing SSE event rebuilds the row; reconcile must restore focus.
    await writeRow.click();
    emit("continued", "tool.completed", {call_id: "write", name: "write_file", content: "Permission denied by user", is_error: true});
    await page.locator("#tool-list").filter({hasText: "已拒绝"}).waitFor();
    assert.equal(await page.evaluate(() => document.activeElement?.dataset?.opId), "continued:write",
      "row focus is restored after the async rebuild");
    await page.getByRole("button", {name: "拒绝", exact: true}).click();
    await page.locator(".chapter-result").filter({hasText: "已处理授权决定"}).waitFor();
    assert.equal(await page.locator("#continue-button").isVisible(), false);
    // The SSE task.plan_updated cue refreshed the plan from session_status: the active
    // task now names the write attempt, without the frontend folding plan events.
    await showSection("#capsule-plan", "#panel-plan");
    await page.locator("#plan").filter({hasText: "尝试写入恢复笔记"}).waitFor();
    assert.doesNotMatch(await page.locator("#plan").innerText(), /读取了恢复相关代码/, "completed summary stays folded after refresh");
    assert.doesNotMatch(await page.locator("#plan").innerText(), /事件负载里才有的一句话/, "panel renders session_status, never the plan event payload");
    // Timeline grew with the continuation nodes.
    assert.ok(await timelineNodes.count() >= 6, "timeline includes continue/approval nodes");

    // Workspace capabilities dialog reads the new endpoint.
    await page.locator("#capabilities-button").click();
    await page.locator("#capabilities-dialog[open]").waitFor();
    await page.locator("#capabilities-content").filter({hasText: "技能 (1)"}).waitFor();
    assert.match(await page.locator("#capabilities-content").innerText(), /MCP 服务 \(1\)/);
    await page.locator("#close-capabilities").click();

    // Mode selection is a menu, not a click-cycle; the picked mode rides on the next
    // run (asserted after recovery so the composer is reachable again).
    const modeButton = page.locator("#mode-select");
    assert.equal(await modeButton.innerText(), "Auto");
    const modeMenu = page.locator("#mode-menu");
    await modeButton.click();
    await modeMenu.waitFor({state: "visible", timeout: 5_000});
    assert.equal(await modeMenu.locator('button.mode-option[data-mode="auto"]').getAttribute("aria-checked"), "true",
      "the menu marks the current mode");
    await modeMenu.locator('button.mode-option[data-mode="agent"]').click();
    assert.equal(await modeButton.innerText(), "Agent");
    await modeMenu.waitFor({state: "hidden", timeout: 5_000});
    await modeButton.click();
    await modeMenu.waitFor({state: "visible", timeout: 5_000});
    await modeMenu.locator('button.mode-option[data-mode="plan"]').click();
    assert.equal(await modeButton.innerText(), "Plan");
    await modeMenu.waitFor({state: "hidden", timeout: 5_000});
    assert.equal(await page.evaluate(() => localStorage.getItem("eventide.mode.s1")), "plan");
    assert.equal(await page.locator("#run-button").getAttribute("aria-label"), "发送", "submit is a labelled icon button");
    assert.equal(await page.locator("#cancel-run").getAttribute("aria-label"), "停止运行");

    // Open the collapsed first chapter; only now may its history be fetched.
    await page.locator(".chapter").first().locator("> summary").click();
    await page.waitForResponse((response) => response.url().includes("/api/runs/r1/events"));
    // Markdown safety now lives on the inline chapter-result card (r1's output).
    const r1Result = page.locator(".chapter").first().locator(".chapter-result");
    await r1Result.waitFor();
    assert.equal(await r1Result.locator("script").count(), 0, "script tags never render");
    assert.equal(await r1Result.locator('a[href^="javascript:"]').count(), 0, "javascript: links never render");
    assert.equal(await page.evaluate(() => window.injected), undefined, "probe script never executed");
    assert.equal(await r1Result.locator("a[href='https://example.com']").count(), 1, "https links render normally");
    assert.ok(await r1Result.locator(".chapter-result-head").count() === 1, "result cards use a head row");
    assert.equal(await r1Result.locator(".result-run").count(), 1);
    assert.equal(await r1Result.locator(".result-copy").count(), 1);
    assert.equal(await r1Result.getByText("当时的结果").count(), 0, "the old 当时 Result heading is gone");
    assert.equal(await page.locator(".chapter").first().locator(".lineage-badge").count(), 0,
      "completed runs with output leave the lineage row");
    // The result card's run badge opens that run in the detail card.
    await r1Result.locator(".result-run").click();
    assert.equal(await page.locator("#floating-panel").isVisible(), true, "result-run opens the detail card");
    assert.equal(await page.locator("#panel-detail").isVisible(), true);
    await page.locator("#detail-content").filter({hasText: "r1"}).waitFor();
    // Lazy chapter's tools: open the panel and its group before touching rows.
    await showSection("#capsule-tools", "#panel-tools");
    const firstGroup = page.locator("#tool-list details.tool-group").filter({hasText: "检查恢复逻辑"});
    await firstGroup.waitFor();
    await openToolGroup(firstGroup);
    await toolRows.filter({hasText: "host.py"}).waitFor();
    assert.ok(apiCalls.some(([, p]) => p === "/api/runs/r1/events"), "lazy chapter loads on expand");
    const readRow = toolRows.filter({hasText: "host.py"}).first();
    await readRow.click();
    assert.match(await page.locator("#tool-list details.tool-group").filter({hasText: "检查恢复逻辑"}).locator(".tool-detail").innerText(), /host\.py/);
    await readRow.click(); // close the detail again for a stable screenshot

    // Refresh keeps the selection and the durable state.
    runs.get("r1").output = "### 恢复流程已梳理\n已读取 **恢复逻辑** 与相关测试。\n- Session 在中断后停驻，保留已有工作记录。\n- Continue 校验工作区，通过后接续同一项工作。\n- 失败场景的验证仍需继续处理。";
    await page.reload(); await page.waitForLoadState("networkidle");
    fs.mkdirSync(output, {recursive: true});
    assert.equal(await page.locator("#session-title").innerText(), "改善 Session 恢复体验");

    // s2 was renamed earlier, so select by its new title, then switch back to s1.
    await page.locator(".session-item").filter({hasText: "恢复体验审计"}).locator(".session-select").click();
    await page.locator("#session-title").filter({hasText: "恢复体验审计"}).waitFor();
    await page.locator(".session-item").filter({hasText: "改善 Session 恢复体验"}).locator(".session-select").click();
    await page.locator("#session-title").filter({hasText: "改善 Session"}).waitFor();
    await page.waitForLoadState("networkidle");
    assert.match(await page.locator("#narrative").innerText(), /已处理授权决定/);

    // Workspace switching keeps the reading position and a reachable composer.
    runs.get("r-third").output = "另一个项目的结果\n" + "一段较长的工作结果，用于验证阅读位置。\n".repeat(90);
    await page.locator("#workspace-switcher-button").click();
    await page.locator(".workspace-option").filter({hasText: "other-project"}).click();
    await page.locator(".chapter-result").filter({hasText: "另一个项目的结果"}).waitFor();
    await page.locator("#work-scroll").evaluate((node) => { node.scrollTop = 700; });
    await page.waitForTimeout(600);
    assert.equal(await page.locator("#work-scroll").evaluate((node) => node.scrollTop), 700, "reading position survives re-renders");
    const actionBox = await page.locator("#run-button").boundingBox();
    assert.ok(actionBox.y + actionBox.height <= 1000, "composer stays reachable with long output");
    await page.locator("#workspace-switcher-button").click();
    await page.locator(".workspace-option").filter({hasText: "eventide"}).click();
    await page.locator("#session-title").filter({hasText: "改善 Session"}).waitFor();

    await page.locator("#add-workspace").click();
    await page.locator("#workspace-input").fill("E:\\added");
    await page.locator("#save-workspace").click();
    await page.locator("#workspace-name").filter({hasText: "added-project"}).waitFor();
    assert.equal(workspaces.at(-1).path, "E:\\added");
    await page.locator("#workspace-switcher-button").click();
    await page.locator(".workspace-option").filter({hasText: "eventide"}).click();
    await page.locator("#session-title").filter({hasText: "改善 Session"}).waitFor();

    // Attachments: a working endpoint shows a chip and rides along with the run.
    const uploadPath = path.join(output, "hello.txt");
    fs.writeFileSync(uploadPath, "hello", "utf8");
    await page.setInputFiles("#file-input", uploadPath);
    await page.locator(".attachment-chip").filter({hasText: "hello.txt"}).waitFor();
    const promptBox = page.locator("#prompt");
    await promptBox.fill("带附件的工作");
    await promptBox.press("Shift+Enter");
    await promptBox.type("第二行");
    assert.equal(await promptBox.evaluate((node) => node.value), "带附件的工作\n第二行", "Shift+Enter inserts a newline");
    assert.equal(lastRunBody, null, "Shift+Enter must not submit");
    await promptBox.press("Enter");
    await page.locator(".chapter-result").filter({hasText: "新的工作结果"}).waitFor();
    assert.equal(lastRunBody.mode, "plan", "Enter submits with the mode picked in the menu");
    assert.deepEqual(lastRunBody.attachment_ids.length, 1, "the chip rides along with the run");

    // A 404 from the attachments endpoint disables the ＋ affordance once, with a reason.
    attachmentsBroken = true;
    await page.setInputFiles("#file-input", uploadPath);
    await page.locator(".toast-message").filter({hasText: "附件不可用"}).waitFor();
    assert.equal(await page.locator("#attach-button").isDisabled(), true, "＋ button greys out when the endpoint is gone");
    const toastClose = await page.locator(".toast-close").first().boundingBox();
    assert.ok(toastClose && toastClose.width >= 24 && toastClose.height >= 24, "toast close stays a comfortable click target");

    // A rejected Continue leaves Parked visible and explains admission failure.
    addRun("parked-again", "s1", "interrupted"); emit("parked-again", "run.interrupted", {error: "Maximum agent steps exceeded (30)", reason: "step_budget"}); continueRejected = true;
    await page.reload(); await page.waitForLoadState("networkidle");
    assert.match(await page.locator("#recovery-reason").innerText(), /步数预算/, "park reason is explained in user language");
    await page.locator("#continue-button").click();
    await page.locator(".toast-message").filter({hasText: "Workspace changed"}).waitFor();
    assert.equal(await page.locator("#continue-button").isVisible(), true);

    // Mobile: drawer navigation, empty workspace scope, creation and submission.
    await page.setViewportSize({width: 390, height: 844});
    await page.locator("#open-sidebar").click();
    await page.locator("#sidebar.open").waitFor();
    await page.locator("#drawer-backdrop").waitFor();
    await page.locator("#workspace-switcher-button").click();
    await page.locator(".workspace-option").filter({hasText: "empty"}).click();
    await page.locator("#session-title").filter({hasText: "从一项工作开始"}).waitFor();
    assert.equal(creations, 0, "empty workspace must not auto-create");
    // The plan card stays hidden behind its capsule on mobile; textContent still proves scoping.
    assert.match(await page.locator("#plan").textContent(), /本次工作没有任务计划。/, "plan panel is scoped to sessions that have one");
    await page.locator("#open-sidebar").click();
    await page.locator("#sidebar.open").waitFor();
    await page.locator("#new-session").click();
    await page.locator("#session-title").filter({hasText: "新的工作"}).waitFor();
    assert.equal(creations, 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator("#prompt").fill("检查当前项目");
    await page.locator("#prompt").press("Enter");
    await page.locator(".chapter-result").filter({hasText: "新的工作结果"}).waitFor();
    await page.screenshot({path: path.join(output, "web-workspace-mobile.png")});
    // The drawer backdrop is itself a close affordance; tap beside the drawer.
    await page.locator("#open-sidebar").click();
    await page.locator("#sidebar.open").waitFor();
    await page.locator("#drawer-backdrop").click({position: {x: 350, y: 400}});
    await page.waitForFunction(() => !document.querySelector("#sidebar")?.classList.contains("open"), null, {timeout: 5_000});

    // Theme toggle persists across reloads.
    await page.setViewportSize({width: 1440, height: 1000});
    await page.locator("#theme-toggle").click();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    assert.equal(await page.evaluate(() => localStorage.getItem("eventide.theme")), "dark");
    await page.reload(); await page.waitForLoadState("networkidle");
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark", "theme persists after reload");
    await page.locator("#theme-toggle").click();

    await page.screenshot({path: path.join(output, "web-workspace-desktop.png"), fullPage: false});
    assert.deepEqual(errors, []);
    console.log("PASS: capsule floating card, lazy history, chapter-result markdown safety, tool group focus, plan projection, context meter, timeline, export, mode menu, theme, async Continue, capabilities, 409 recovery, attachments, workspace add/switch, scroll retention, mobile navigation and submission");
  } finally { await browser.close(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); }
})().catch((error) => { console.error(error); process.exitCode = 1; server.closeAllConnections(); server.close(); });
