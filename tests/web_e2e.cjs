#!/usr/bin/env node
/**
 * Web E2E driver: two phases against the real server from tests/e2e_server.py.
 *
 * Phase 1 (no browser): connectivity checklist over fetch — status codes plus
 * response schema for every endpoint the Web UI v2 contract depends on.
 * Phase 2 (Playwright): UI journeys against the v2 console; skipped with
 * `UI-V2-NOT-DEPLOYED` while the new frontend is not merged, and with
 * `SKIP: playwright 不可用` when the module is absent.
 *
 * Usage:
 *   EVENTIDE_PLAYWRIGHT_MODULE="C:/Users/22945/AppData/Roaming/npm/node_modules/playwright" \
 *   EVENTIDE_E2E_PYTHON=".venv/Scripts/python" node tests/web_e2e.cjs
 *
 * Exit codes: 0 pass or graceful skip; 1 assertion failure.
 * Checks tagged v2 below assert the frontend contract in docs/api-contract.md;
 * while the backend waves have not landed, a missing route or missing field is
 * reported as PENDING (exit 0) instead of FAIL. Set EVENTIDE_E2E_STRICT_V2=1
 * to turn pendings into failures once Wave 3 integration is in place.
 * Scenario marker texts must stay in sync with tests/e2e_server.py.
 */
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const REPO_ROOT = path.resolve(__dirname, "..");
const SHOT_DIR = path.join(REPO_ROOT, ".task_outputs", "e2e");
const SERVER_START_TIMEOUT_MS = 30_000;
const RUN_TIMEOUT_MS = 90_000;

const MARK = {
  multi: "多步任务",
  approval: "审批场景",
  park: "停驻场景",
  attachment: "附件场景",
};

// ---------------------------------------------------------------------------
// shared state and helpers
// ---------------------------------------------------------------------------

const state = { port: 0, workspace: "", child: null, BASE: "" };

function log(message) {
  console.log(message);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function samePath(a, b) {
  const norm = (value) => path.resolve(String(value)).toLowerCase().replace(/\\/g, "/");
  return norm(a) === norm(b);
}

class CheckFail extends Error {}
class CheckPending extends Error {}

function fail(message) {
  throw new CheckFail(message);
}

function pending(message) {
  throw new CheckPending(message);
}

const checks = [];

async function check(name, fn) {
  const entry = { name, status: "pass", detail: "" };
  checks.push(entry);
  try {
    await fn(entry);
  } catch (err) {
    entry.status = err instanceof CheckPending ? "pending" : "fail";
    entry.detail = String((err && err.message) || err);
  }
  const mark = { pass: "PASS", pending: "PENDING", fail: "FAIL" }[entry.status];
  log(`[${mark}] ${name}${entry.detail ? ` — ${entry.detail}` : ""}`);
}

async function api(method, pathName, body) {
  const hasBody = body !== undefined;
  const resp = await fetch(`${state.BASE}${pathName}`, {
    method,
    headers: hasBody ? { "Content-Type": "application/json" } : undefined,
    body: hasBody ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    // non-JSON body stays null
  }
  return { status: resp.status, json, text, contentType: resp.headers.get("content-type") || "" };
}

function isRouteMissing(resp) {
  // FastAPI's default answer for a path that no router handles in this build.
  return resp.status === 404 && (resp.json === null || resp.json?.detail === "Not Found");
}

async function pollRun(runId) {
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  let last = null;
  while (Date.now() < deadline) {
    const resp = await api("GET", `/api/runs/${runId}`);
    if (resp.status !== 200) fail(`GET /api/runs/${runId} -> ${resp.status} ${resp.text.slice(0, 200)}`);
    last = resp.json;
    if (last.status && last.status !== "running") return last;
    await sleep(300);
  }
  fail(`轮询 run ${runId} 超时（${RUN_TIMEOUT_MS}ms），最后状态：${JSON.stringify(last)}`);
}

function parseSseChunk(chunk) {
  let type = null;
  let data = "";
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event: ")) type = line.slice(7).trim();
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!type) return null;
  let payload = null;
  try {
    payload = JSON.parse(data);
  } catch {
    // keep null payload for non-JSON data lines
  }
  return { type, data: payload };
}

async function collectSse(pathName, timeoutMs = 15_000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const events = [];
  try {
    const resp = await fetch(`${state.BASE}${pathName}`, { signal: controller.signal });
    if (!resp.ok) fail(`SSE ${pathName} -> ${resp.status}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const event = parseSseChunk(buffer.slice(0, boundary));
        if (event) events.push(event);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch (err) {
    if (events.length === 0) throw err;
  } finally {
    clearTimeout(timer);
  }
  return events;
}

// ---------------------------------------------------------------------------
// server lifecycle
// ---------------------------------------------------------------------------

function resolvePython() {
  if (process.env.EVENTIDE_E2E_PYTHON) return process.env.EVENTIDE_E2E_PYTHON;
  for (const candidate of [".venv/Scripts/python.exe", ".venv/Scripts/python"]) {
    const absolute = path.join(REPO_ROOT, candidate);
    if (fs.existsSync(absolute)) return absolute;
  }
  return "python";
}

function pumpStream(stream, tag) {
  stream.on("data", (chunk) => {
    for (const line of String(chunk).split("\n")) {
      if (line.trim()) log(`${tag} ${line}`);
    }
  });
}

async function startServer() {
  const pythonExe = resolvePython();
  const child = spawn(pythonExe, [path.join(__dirname, "e2e_server.py"), "--port", "0"], {
    cwd: REPO_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
  });
  state.child = child;
  pumpStream(child.stderr, "[server:err]");
  // stdout is pumped below together with marker parsing.

  const info = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`等待 E2E_PORT 超时（${SERVER_START_TIMEOUT_MS}ms）`)),
      SERVER_START_TIMEOUT_MS,
    );
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`服务进程提前退出（code=${code}）`));
    });
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      log(`[server] ${line}`);
      if (line.startsWith("E2E_WORKSPACE=")) state.workspace = line.slice("E2E_WORKSPACE=".length);
      if (line.startsWith("E2E_PORT=")) {
        clearTimeout(timer);
        child.removeAllListeners("exit");
        resolve({ port: Number(line.slice("E2E_PORT=".length)) });
      }
    });
  });

  state.port = info.port;
  state.BASE = `http://127.0.0.1:${state.port}`;
  if (!state.workspace) fail("服务未打印 E2E_WORKSPACE");
}

async function shutdownServer() {
  const child = state.child;
  if (!child || child.exitCode !== null) return;
  try {
    await fetch(`${state.BASE}/__e2e/shutdown`, { method: "POST" });
  } catch {
    // fall through to the forced kill below
  }
  const graceful = new Promise((resolve) => child.once("exit", resolve));
  const winner = await Promise.race([graceful, sleep(6000).then(() => "timeout")]);
  if (winner === "timeout") {
    try {
      spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
    } catch {
      // nothing else to try
    }
    await Promise.race([graceful, sleep(4000)]);
  }
}

process.on("SIGINT", () => {
  if (state.child && state.child.exitCode === null) {
    try {
      spawnSync("taskkill", ["/PID", String(state.child.pid), "/T", "/F"], { stdio: "ignore" });
    } catch {
      // ignore
    }
  }
  process.exit(1);
});

// ---------------------------------------------------------------------------
// phase 1: connectivity checklist
// ---------------------------------------------------------------------------

async function phase1() {
  log("=== 阶段 1：接口连通清单 ===");
  const wsRoot = path.dirname(state.workspace);
  const plainPath = path.join(wsRoot, "plain");
  const capsPath = path.join(wsRoot, "caps");
  const shared = { mainWsId: "", capsWsId: "", sessionId: "", runId: "", attachmentId: "" };

  await check("GET /healthz", async () => {
    const resp = await api("GET", "/healthz");
    if (resp.status !== 200) fail(`状态码 ${resp.status} != 200`);
    if (resp.json?.status !== "ok" || resp.json?.runtime !== "eventide") {
      fail(`响应体 ${JSON.stringify(resp.json)}`);
    }
  });

  await check("GET /api/workspaces", async () => {
    const resp = await api("GET", "/api/workspaces");
    if (resp.status !== 200 || !Array.isArray(resp.json)) {
      fail(`状态码 ${resp.status}，响应 ${resp.text.slice(0, 200)}`);
    }
    const main = resp.json.find((item) => samePath(item.path, state.workspace));
    if (!main) fail(`工作区列表缺少主 fixture ${state.workspace}：${JSON.stringify(resp.json)}`);
    if (typeof main.workspace_id !== "string" || !main.workspace_id.startsWith("ws_")) {
      fail(`workspace_id 异常：${main.workspace_id}`);
    }
    shared.mainWsId = main.workspace_id;
  });

  await check("POST /api/workspaces", async () => {
    for (const target of [plainPath, capsPath]) {
      const resp = await api("POST", "/api/workspaces", { path: target });
      if (resp.status !== 201) fail(`POST ${target} -> ${resp.status}：${resp.text.slice(0, 200)}`);
      if (typeof resp.json?.workspace_id !== "string") fail(`响应缺少 workspace_id：${resp.text.slice(0, 200)}`);
      if (samePath(target, capsPath)) shared.capsWsId = resp.json.workspace_id;
    }
    if (!shared.capsWsId) fail("未拿到 caps fixture 的 workspace_id");
  });

  await check("GET /api/workspaces/{id}/capabilities（skills/mcp/paths）", async () => {
    const resp = await api("GET", `/api/workspaces/${shared.capsWsId}/capabilities`);
    if (isRouteMissing(resp)) pending("端点未随当前后端构建部署（v2）");
    if (resp.status !== 200) fail(`状态码 ${resp.status}：${resp.text.slice(0, 200)}`);
    const caps = resp.json;
    if (!Array.isArray(caps.skills)) fail(`skills 不是数组：${JSON.stringify(caps)}`);
    const demo = caps.skills.find((skill) => skill.name === "demo");
    if (!demo) fail(`skills 不含 demo：${JSON.stringify(caps.skills)}`);
    if (typeof demo.description !== "string" || !demo.description.trim()) fail("demo 技能缺少 description");
    if (!Array.isArray(caps.mcp)) fail(`mcp 不是数组：${JSON.stringify(caps)}`);
    const docs = caps.mcp.find((server) => server.name === "docs");
    if (!docs) fail(`mcp 不含 docs：${JSON.stringify(caps.mcp)}`);
    if (docs.transport !== "stdio") fail(`docs transport=${docs.transport}，期望 stdio`);
    if (!caps.paths || typeof caps.paths.mcp_config !== "string" || typeof caps.paths.skills_dir !== "string") {
      fail(`paths 缺少 mcp_config/skills_dir：${JSON.stringify(caps.paths)}`);
    }
  });

  await check("GET /api/runtime/settings（四字段）", async () => {
    const resp = await api("GET", "/api/runtime/settings");
    if (isRouteMissing(resp)) pending("端点未随当前后端构建部署（v2）");
    if (resp.status !== 200) fail(`状态码 ${resp.status}：${resp.text.slice(0, 200)}`);
    for (const key of ["context_limit", "approval_timeout", "max_steps", "task_max_steps"]) {
      if (typeof resp.json?.[key] !== "number") fail(`缺少数值字段 ${key}：${JSON.stringify(resp.json)}`);
    }
    if (resp.json.max_steps !== 6) fail(`max_steps=${resp.json.max_steps}，期望 6（harness 设定）`);
    if (resp.json.approval_timeout !== 60) fail(`approval_timeout=${resp.json.approval_timeout}，期望 60`);
  });

  await check("POST /api/sessions", async () => {
    const resp = await api("POST", "/api/sessions", { workspace_id: shared.mainWsId });
    if (resp.status !== 201) fail(`状态码 ${resp.status}：${resp.text.slice(0, 200)}`);
    if (typeof resp.json?.session_id !== "string") fail(`响应缺少 session_id：${resp.text.slice(0, 200)}`);
    shared.sessionId = resp.json.session_id;
  });

  await check("POST /api/sessions/{id}/runs -> 202 + run_id", async () => {
    const resp = await api("POST", `/api/sessions/${shared.sessionId}/runs`, {
      prompt: `${MARK.multi}：请制定计划并执行`,
    });
    if (resp.status !== 202) fail(`状态码 ${resp.status}：${resp.text.slice(0, 200)}`);
    if (typeof resp.json?.run_id !== "string") fail(`响应缺少 run_id：${resp.text.slice(0, 200)}`);
    shared.runId = resp.json.run_id;
  });

  await check("轮询 GET /api/runs/{id} 到 completed", async () => {
    const run = await pollRun(shared.runId);
    if (run.status !== "completed") fail(`run 状态 ${run.status}：${JSON.stringify(run)}`);
    if (!String(run.output).includes("多步任务已完成")) fail(`output=${JSON.stringify(run.output)}`);
    if (run.steps !== 5) fail(`steps=${run.steps}，期望 5`);
    if (run.tool_calls !== 4) fail(`tool_calls=${run.tool_calls}，期望 4`);
  });

  await check("GET /api/sessions/{id} task_state 3 项全 completed", async () => {
    const resp = await api("GET", `/api/sessions/${shared.sessionId}`);
    if (resp.status !== 200) fail(`状态码 ${resp.status}`);
    const tasks = resp.json?.task_state?.tasks;
    if (!Array.isArray(tasks) || tasks.length !== 3) {
      fail(`task_state.tasks=${JSON.stringify(tasks)}`);
    }
    const notDone = tasks.filter((task) => task.status !== "completed");
    if (notDone.length) fail(`未完成任务：${JSON.stringify(notDone.map((task) => task.id))}`);
    if (resp.json.task_state.active_task_id !== null) fail(`active_task_id=${resp.json.task_state.active_task_id}`);
  });

  await check("latest_run.usage 非零", async () => {
    const resp = await api("GET", `/api/sessions/${shared.sessionId}`);
    const latest = resp.json?.latest_run;
    if (!latest) fail("latest_run 缺失");
    if (!("usage" in latest)) pending("usage 字段未随当前后端构建部署（v2）");
    const usage = latest.usage || {};
    if (!(usage.input_tokens > 0) || !(usage.output_tokens > 0)) {
      fail(`usage=${JSON.stringify(usage)}`);
    }
  });

  await check("GET /api/runs/{id}/events SSE 含 model.request", async () => {
    const events = await collectSse(`/api/runs/${shared.runId}/events`);
    const modelRequests = events.filter((event) => event.type === "model.request");
    if (!modelRequests.length) {
      fail(`事件流无 model.request；实际类型：${[...new Set(events.map((event) => event.type))].join(",")}`);
    }
    const first = modelRequests[0].data?.payload || {};
    if (!("request_chars" in first)) pending("request_chars 字段未随当前后端构建部署（v2）");
    if (!(first.request_chars > 0)) fail(`request_chars=${JSON.stringify(first.request_chars)}`);
    if (!events.some((event) => event.type === "run.completed")) fail("事件流无 run.completed");
  });

  await check("POST /api/sessions/{id}/attachments -> 201", async () => {
    const resp = await api("POST", `/api/sessions/${shared.sessionId}/attachments`, {
      name: "hello.txt",
      content_base64: Buffer.from("hello", "utf8").toString("base64"),
    });
    if (isRouteMissing(resp)) {
      pending("附件接口未随当前后端构建部署（v2）");
    }
    if (resp.status !== 201) fail(`状态码 ${resp.status}：${resp.text.slice(0, 200)}`);
    const body = resp.json || {};
    if (typeof body.attachment_id !== "string") fail(`响应缺少 attachment_id：${resp.text.slice(0, 200)}`);
    if (body.name !== "hello.txt" || body.size !== 5) fail(`name/size 异常：${JSON.stringify(body)}`);
    shared.attachmentId = body.attachment_id;
  });

  await check("带附件提交 run，message.user 含附件分隔符", async () => {
    if (!shared.attachmentId) pending("依赖附件上传接口");
    const submit = await api("POST", `/api/sessions/${shared.sessionId}/runs`, {
      prompt: `${MARK.attachment}：请阅读附件内容`,
      attachment_ids: [shared.attachmentId],
    });
    if (submit.status !== 202) fail(`提交 run 状态码 ${submit.status}：${submit.text.slice(0, 200)}`);
    const run = await pollRun(submit.json.run_id);
    if (run.status !== "completed") fail(`run 状态 ${run.status}`);
    const events = await collectSse(`/api/runs/${submit.json.run_id}/events`);
    const userMessage = events.find((event) => event.type === "message.user");
    if (!userMessage) fail(`事件流无 message.user：${[...new Set(events.map((event) => event.type))].join(",")}`);
    const content = userMessage.data?.payload?.message?.content;
    if (typeof content !== "string") fail(`message.user.content=${JSON.stringify(content)}`);
    if (!content.includes("--- 附件：hello.txt ---")) fail(`附件分隔符缺失：${JSON.stringify(content.slice(0, 200))}`);
    if (!content.includes("hello")) fail("附件内容未内联");
  });

  await check("GET /api/config/provider", async () => {
    const resp = await api("GET", "/api/config/provider");
    if (resp.status !== 200) fail(`状态码 ${resp.status}`);
    for (const key of ["provider", "model", "api_key_status"]) {
      if (!(key in (resp.json || {}))) fail(`缺少字段 ${key}：${resp.text.slice(0, 200)}`);
    }
  });

  await check("GET /api/runs/{id}/export NDJSON", async () => {
    const resp = await api("GET", `/api/runs/${shared.runId}/export`);
    if (resp.status !== 200) fail(`状态码 ${resp.status}`);
    if (!resp.contentType.startsWith("application/x-ndjson")) fail(`content-type=${resp.contentType}`);
    const lines = resp.text.trim().split("\n");
    if (lines.length < 3) fail(`仅 ${lines.length} 行 NDJSON`);
    const parsed = lines.map((line) => JSON.parse(line));
    if (parsed.some((event) => typeof event.type !== "string")) fail("存在缺少 type 字段的行");
    if (parsed[parsed.length - 1].type !== "run.completed") fail("末行不是 run.completed");
  });

  await check("GET /api/sessions/{id}/attachments 预留接口未实现", async () => {
    const resp = await api("GET", `/api/sessions/${shared.sessionId}/attachments`);
    // POST 已占用同一路径，未实现的 GET 返回 405（预留清单见 api-contract.md）
    if (resp.status !== 405) fail(`状态码 ${resp.status} != 405（预留接口）`);
  });

  await check("停驻场景：park -> continue -> 已恢复完成", async () => {
    const session = await api("POST", "/api/sessions", { workspace_id: shared.mainWsId });
    if (session.status !== 201) fail(`创建会话失败：${session.status}`);
    const sid = session.json.session_id;
    const submit = await api("POST", `/api/sessions/${sid}/runs`, {
      prompt: `${MARK.park}：一直读取文件`,
    });
    if (submit.status !== 202) fail(`提交 run 失败：${submit.status}`);
    const parked = await pollRun(submit.json.run_id);
    if (parked.status !== "interrupted") fail(`run 状态 ${parked.status}，期望 interrupted`);
    if (parked.reason !== "step_budget") fail(`park 原因 ${parked.reason}，期望 step_budget`);
    const record = await api("GET", `/api/sessions/${sid}`);
    if (record.json?.status !== "parked") fail(`会话状态 ${record.json?.status}，期望 parked`);
    const resumed = await api("POST", `/api/sessions/${sid}/continue`);
    if (resumed.status !== 202) fail(`continue 状态码 ${resumed.status}：${resumed.text.slice(0, 200)}`);
    const done = await pollRun(resumed.json.run_id);
    if (done.status !== "completed") fail(`恢复 run 状态 ${done.status}`);
    if (!String(done.output).includes("已恢复完成")) fail(`output=${JSON.stringify(done.output)}`);
  });

  await check("审批场景：ASK 审批 -> 批准 -> 完成", async () => {
    const session = await api("POST", "/api/sessions", { workspace_id: shared.mainWsId });
    if (session.status !== 201) fail(`创建会话失败：${session.status}`);
    const sid = session.json.session_id;
    const submit = await api("POST", `/api/sessions/${sid}/runs`, {
      prompt: `${MARK.approval}：删除临时文件`,
    });
    if (submit.status !== 202) fail(`提交 run 失败：${submit.status}`);
    const deadline = Date.now() + 30_000;
    let approvalId = "";
    while (Date.now() < deadline) {
      const resp = await api("GET", `/api/runs/${submit.json.run_id}`);
      const pending_ = resp.json?.pending_approvals || {};
      const ids = Object.keys(pending_);
      if (ids.length) {
        approvalId = ids[0];
        break;
      }
      if (resp.json?.status && resp.json.status !== "running") break;
      await sleep(300);
    }
    if (!approvalId) fail("审批未出现（pending_approvals 为空）");
    const decision = await api("POST", `/api/runs/${submit.json.run_id}/approvals/${approvalId}`, {
      approved: true,
    });
    if (decision.status !== 200) fail(`审批决定状态码 ${decision.status}`);
    const run = await pollRun(submit.json.run_id);
    if (run.status !== "completed") fail(`run 状态 ${run.status}`);
    if (!String(run.output).includes("审批场景已完成")) fail(`output=${JSON.stringify(run.output)}`);
  });

  const fails = checks.filter((entry) => entry.status === "fail");
  const pendings = checks.filter((entry) => entry.status === "pending");
  if (fails.length) {
    log("\n=== 连通性断言失败 ===");
    for (const entry of [...fails, ...pendings]) {
      log(`- [${entry.status}] ${entry.name}: ${entry.detail}`);
    }
    return 1;
  }
  if (pendings.length) {
    log(`\nCONNECTIVITY PENDING（${pendings.length} 项待后端 v2 集成，非本 harness 缺陷）`);
    for (const entry of pendings) log(`- ${entry.name}: ${entry.detail}`);
    if (process.env.EVENTIDE_E2E_STRICT_V2 === "1") return 1;
    return 0;
  }
  log("\nCONNECTIVITY PASS");
  return 0;
}

// ---------------------------------------------------------------------------
// phase 2: UI journeys (Playwright)
// ---------------------------------------------------------------------------

function loadPlaywright() {
  const candidates = [
    process.env.EVENTIDE_PLAYWRIGHT_MODULE,
    "playwright",
    "C:/Users/22945/AppData/Roaming/npm/node_modules/playwright",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return { pw: require(candidate), used: candidate };
    } catch (err) {
      if (candidate === candidates[candidates.length - 1]) {
        log(`SKIP: playwright 不可用（${err.code || err.message}）`);
        return null;
      }
    }
  }
  return null;
}

async function phase2() {
  log("\n=== 阶段 2：UI 旅程（Playwright） ===");
  const loaded = loadPlaywright();
  if (!loaded) return 0;
  let browser;
  try {
    browser = await loaded.pw.chromium.launch({ headless: true });
  } catch (err) {
    log(`SKIP: playwright chromium 启动失败（${String(err.message).split("\n")[0]}）`);
    return 0;
  }

  const page = await browser.newPage();
  const pageErrors = [];
  page.on("pageerror", (err) => pageErrors.push(String((err && err.stack) || err)));
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const shot = (name) => page.screenshot({ path: path.join(SHOT_DIR, name), fullPage: true });
  const waitText = (text, timeout) =>
    page.waitForFunction(
      (marker) => document.body && document.body.innerText.includes(marker),
      text,
      { timeout },
    );
  // The fake provider keeps per-session step counters (tests/e2e_server.py), so
  // every scenario needs a fresh work record; submitting into the previous
  // scenario's session would skip the approval card and the parking flow.
  const newWork = async () => {
    await page.click("#new-session");
    await page.waitForFunction(
      () => document.querySelector("#session-pill")?.dataset.status === "idle",
      null,
      { timeout: 10_000 },
    );
  };

  try {
    await page.goto(`${state.BASE}/`, { waitUntil: "domcontentloaded", timeout: 20_000 });
    try {
      await page.waitForSelector("#work-panel", { timeout: 5_000 });
    } catch {
      log("UI-V2-NOT-DEPLOYED: skip journeys");
      return 0;
    }

    // -- 添加工作区 ---------------------------------------------------------
    await page.click("#add-workspace");
    await page.waitForSelector("#workspace-input", { state: "visible", timeout: 5_000 });
    await page.fill("#workspace-input", state.workspace);
    await page.click("#save-workspace");
    await waitText(path.basename(state.workspace), 10_000);
    await shot("01-workspace-added.png");

    // -- 新建工作 -----------------------------------------------------------
    const newWorkSelectors = [
      'button:has-text("新建工作")',
      'button:has-text("新建会话")',
      '[data-testid="new-work"]',
      'button:has-text("新建")',
    ];
    let opened = false;
    for (const selector of newWorkSelectors) {
      const button = page.locator(selector).first();
      try {
        await button.waitFor({ state: "visible", timeout: 1_500 });
        await button.click();
        opened = true;
        break;
      } catch {
        // try the next candidate
      }
    }
    if (!opened) fail(`找不到“新建工作”入口，尝试过：${newWorkSelectors.join(" / ")}`);
    await page.waitForSelector("#prompt", { state: "visible", timeout: 10_000 });
    // 新会话必须真正被选中（pill 回到"尚未开始"），否则后续会提交到旧会话上
    await page.waitForFunction(
      () => document.querySelector("#session-pill")?.dataset.status === "idle",
      null,
      { timeout: 10_000 },
    );

    // -- 多步任务 -----------------------------------------------------------
    await page.fill("#prompt", `${MARK.multi}：请制定计划并执行`);
    await page.click("#run-button");
    await page.waitForFunction(
      () => {
        const el = document.querySelector("#plan");
        return Boolean(el && /3\s*\/\s*3/.test(el.innerText));
      },
      null,
      { timeout: 45_000 },
    );
    await page.waitForFunction(
      () => {
        const el = document.querySelector("#tool-list");
        return Boolean(el && el.innerText.includes("read_file") && el.innerText.includes("bash"));
      },
      null,
      { timeout: 15_000 },
    );
    // 全部 4 次工具调用（todo_write/read_file/bash/todo_write）到齐后才算完整
    await page.waitForFunction(
      () => Boolean((document.querySelector("#tool-stats")?.textContent || "").includes("总 4")),
      null,
      { timeout: 10_000 },
    );
    await page.waitForSelector("#outcome", { state: "visible", timeout: 15_000 });
    await shot("02-multistep-done.png");

    // -- 审批场景 -----------------------------------------------------------
    await newWork();
    await page.fill("#prompt", `${MARK.approval}：删除临时文件`);
    await page.click("#run-button");
    const approveSelectors = ['button:has-text("本次允许")', 'button:has-text("允许")'];
    let approved = false;
    for (const selector of approveSelectors) {
      const button = page.locator(selector).first();
      try {
        await button.waitFor({ state: "visible", timeout: 20_000 });
        await shot("03-approval-card.png");
        await button.click();
        approved = true;
        break;
      } catch {
        // try the next candidate
      }
    }
    if (!approved) fail(`审批卡未出现或找不到批准按钮，尝试过：${approveSelectors.join(" / ")}`);
    await waitText("审批场景已完成", 30_000);

    // -- 停驻场景 -----------------------------------------------------------
    await newWork();
    await page.fill("#prompt", `${MARK.park}：一直读取文件`);
    await page.click("#run-button");
    await page.waitForSelector("#recovery", { state: "visible", timeout: 30_000 });
    const reason = (await page.textContent("#recovery-reason")) || "";
    if (!reason.includes("步数预算")) fail(`#recovery-reason=${JSON.stringify(reason)}，未包含“步数预算”`);
    await shot("04-recovery.png");
    await page.click("#continue-button");
    await waitText("已恢复完成", 30_000);

    // -- 附件 ---------------------------------------------------------------
    const uploadPath = path.join(SHOT_DIR, "hello.txt");
    fs.writeFileSync(uploadPath, "hello", "utf8");
    await page.setInputFiles("#file-input", uploadPath);
    await waitText("hello.txt", 5_000); // chip 出现
    await shot("05-attachment-chip.png");
    await page.fill("#prompt", `${MARK.attachment}：请阅读附件内容`);
    await page.click("#run-button");
    await waitText("附件已读取", 45_000);

    // -- 主题切换 -----------------------------------------------------------
    await page.click("#theme-toggle");
    await page.waitForFunction(
      () => document.documentElement.getAttribute("data-theme") === "dark",
      null,
      { timeout: 5_000 },
    );
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.documentElement.getAttribute("data-theme") === "dark",
      null,
      { timeout: 5_000 },
    );
    await shot("06-theme-dark.png");

    if (pageErrors.length) {
      log(`UI JOURNEY FAIL: 捕获 ${pageErrors.length} 个 pageerror`);
      for (const message of pageErrors) log(`  pageerror: ${message.split("\n")[0]}`);
      return 1;
    }
    log(`UI JOURNEY PASS（截图见 ${SHOT_DIR}）`);
    return 0;
  } catch (err) {
    await shot("99-failure.png").catch(() => {});
    log(`UI JOURNEY FAIL: ${String((err && err.message) || err)}`);
    if (pageErrors.length) {
      for (const message of pageErrors) log(`  pageerror: ${message.split("\n")[0]}`);
    }
    return 1;
  } finally {
    await browser.close();
  }
}

// ---------------------------------------------------------------------------
// entry
// ---------------------------------------------------------------------------

async function main() {
  await startServer();
  log(`服务就绪：${state.BASE}（主 fixture：${state.workspace}）`);
  try {
    const code1 = await phase1();
    if (code1 !== 0) return code1;
    return await phase2();
  } finally {
    await shutdownServer();
  }
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    log(`E2E DRIVER FAIL: ${String((err && err.stack) || err)}`);
    shutdownServer()
      .catch(() => {})
      .finally(() => process.exit(1));
  });
