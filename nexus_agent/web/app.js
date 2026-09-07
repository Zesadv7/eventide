const $ = (selector) => document.querySelector(selector);
const state = { sessionId: null, runId: null, events: 0, source: null, configured: false };

const EVENT_NAMES = {
  "run.started": "运行开始", "model.request": "请求模型", "model.response": "模型响应",
  "model.retry": "模型重试", "tool.request": "调用工具", "tool.result": "工具结果",
  "approval.required": "等待审批", "approval.resolved": "审批完成",
  "context.compacted": "上下文压缩", "run.completed": "运行完成", "run.failed": "运行失败",
};

async function request(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* 保留 HTTP 状态 */ }
    throw new Error(detail);
  }
  return response.json();
}

async function bootstrap() {
  try {
    await request("/healthz");
    $("#health").textContent = "服务在线";
    $(".pulse").classList.add("online");
    const session = await request("/api/sessions", { method: "POST" });
    state.sessionId = session.session_id;
    $("#session-id").textContent = state.sessionId;
    await loadProviderConfig();
  } catch (_) {
    $("#health").textContent = "服务离线";
  }
}

function renderProviderStatus(config) {
  state.configured = config.api_key_configured;
  const button = $("#model-config-button");
  button.classList.toggle("configured", state.configured);
  $("#model-state").textContent = config.configuration_error
    ? "配置异常"
    : state.configured ? config.model : "需要配置";
  $("#provider").value = config.provider;
  $("#model").value = config.model || "";
  $("#base-url").value = config.base_url || "";
  $("#key-hint").textContent = config.api_key_status === "decrypt_error"
    ? config.configuration_error
    : state.configured
      ? `已有安全保存的密钥 · ${config.api_key_source === "sqlite_encrypted" ? "SQLite 加密存储" : "来自环境变量"}`
      : "尚未保存 API Key";
  $("#key-hint").classList.toggle("error", config.api_key_status === "decrypt_error");
}

async function loadProviderConfig() {
  renderProviderStatus(await request("/api/config/provider"));
}

function providerBody() {
  const body = {
    provider: $("#provider").value,
    model: $("#model").value.trim(),
    base_url: $("#base-url").value.trim() || null,
  };
  const key = $("#api-key").value.trim();
  if (key) body.api_key = key;
  return body;
}

function setConfigMessage(text, success = false) {
  const message = $("#config-message");
  message.className = success ? "config-message success" : "config-message";
  message.textContent = text;
}

async function saveProviderConfig(event) {
  event.preventDefault();
  const button = $("#save-model-config");
  setConfigMessage("正在加密并保存配置……");
  button.disabled = true;
  try {
    const config = await request("/api/config/provider", {
      method: "PUT", body: JSON.stringify(providerBody()),
    });
    $("#api-key").value = "";
    renderProviderStatus(config);
    setConfigMessage("配置已保存并立即生效", true);
  } catch (error) {
    setConfigMessage(error.message);
  } finally {
    button.disabled = false;
  }
}

async function testProviderConfig() {
  if (!$("#model-form").reportValidity()) return;
  const button = $("#test-model-config");
  const result = $("#connection-result");
  result.hidden = false;
  result.className = "connection-result pending";
  result.textContent = "正在向模型发送最小推理请求，最长等待 30 秒……";
  button.disabled = true;
  try {
    const probe = await request("/api/config/provider/test", {
      method: "POST", body: JSON.stringify(providerBody()),
    });
    result.className = `connection-result ${probe.success ? "success" : "failure"}`;
    result.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = probe.message;
    const detail = document.createElement("span");
    detail.textContent = `${probe.model} · ${Math.round(probe.latency_ms)} ms`;
    result.append(title, detail);
  } catch (error) {
    result.className = "connection-result failure";
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function clearApiKey() {
  if (!confirm("确定清除已加密保存的 API Key 吗？环境变量中的 Key 不会被删除。")) return;
  try {
    const body = { ...providerBody(), clear_api_key: true };
    delete body.api_key;
    const config = await request("/api/config/provider", { method: "PUT", body: JSON.stringify(body) });
    $("#api-key").value = "";
    renderProviderStatus(config);
    setConfigMessage("已清除数据库中的 API Key", true);
  } catch (error) { setConfigMessage(error.message); }
}

async function resetProviderConfig() {
  if (!confirm("确定删除 Web 保存的模型配置，并恢复服务启动时的环境变量配置吗？")) return;
  try {
    const config = await request("/api/config/provider", { method: "DELETE" });
    $("#api-key").value = "";
    renderProviderStatus(config);
    setConfigMessage("已恢复启动配置", true);
  } catch (error) { setConfigMessage(error.message); }
}

function compactPayload(event) {
  const payload = { ...event.payload };
  if (payload.text?.length > 500) payload.text = `${payload.text.slice(0, 500)}…`;
  if (payload.content?.length > 500) payload.content = `${payload.content.slice(0, 500)}…`;
  return JSON.stringify(payload, null, 2);
}

function addEvent(event) {
  if (state.events === 0) $("#timeline").replaceChildren();
  state.events += 1;
  $("#event-count").textContent = `${String(state.events).padStart(3, "0")} 个事件`;
  const row = $("#event-template").content.firstElementChild.cloneNode(true);
  const kind = event.type.split(".")[0];
  row.dataset.kind = event.type.includes("failed") ? "failed" : kind;
  row.querySelector("time").textContent = new Date(event.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  row.querySelector(".event-title").textContent = EVENT_NAMES[event.type] || event.type;
  row.querySelector(".event-detail").textContent = compactPayload(event);
  if (event.type === "approval.required") {
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    actions.innerHTML = '<button type="button">本次允许</button><button type="button" class="deny">拒绝</button>';
    const [allow, deny] = actions.querySelectorAll("button");
    allow.onclick = () => resolveApproval(event.payload.approval_id, true, actions);
    deny.onclick = () => resolveApproval(event.payload.approval_id, false, actions);
    row.querySelector(".event-body").append(actions);
  }
  $("#timeline").append(row);
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function resolveApproval(approvalId, approved, container) {
  try {
    await request(`/api/runs/${state.runId}/approvals/${approvalId}`, {
      method: "POST", body: JSON.stringify({ approved }),
    });
    container.textContent = approved ? "已允许" : "已拒绝";
  } catch (error) { container.textContent = error.message; }
}

async function finishRun() {
  const run = await request(`/api/runs/${state.runId}`);
  $("#metric-state").textContent = run.status === "completed" ? "已完成" : "失败";
  $("#metric-steps").textContent = String(run.steps).padStart(2, "0");
  $("#metric-tools").textContent = String(run.tool_calls).padStart(2, "0");
  $("#metric-latency").textContent = `${Math.round(run.duration_ms)} ms`;
  $("#metric-tokens").textContent = Object.values(run.usage).reduce((a, b) => a + b, 0) || "—";
  $("#answer").textContent = run.output || run.error || "模型没有返回最终回答。";
  $("#answer-panel").hidden = false;
  $("#run-button").disabled = false;
}

async function execute(prompt) {
  if (!state.sessionId) await bootstrap();
  if (!state.configured) {
    setConfigMessage("请先配置 API Key，再开始运行任务");
    $("#model-dialog").showModal();
    return;
  }
  state.events = 0;
  $("#answer-panel").hidden = true;
  $("#metric-state").textContent = "运行中";
  $("#run-button").disabled = true;
  const run = await request(`/api/sessions/${state.sessionId}/runs`, {
    method: "POST", body: JSON.stringify({ prompt }),
  });
  state.runId = run.run_id;
  state.source?.close();
  state.source = new EventSource(`/api/runs/${state.runId}/events`);
  ["run.started", "model.request", "model.response", "model.retry", "tool.request", "tool.result",
   "approval.required", "approval.resolved", "context.compacted", "run.completed", "run.failed"]
    .forEach(type => state.source.addEventListener(type, (message) => {
      addEvent(JSON.parse(message.data));
      if (["run.completed", "run.failed"].includes(type)) { state.source.close(); finishRun(); }
    }));
  state.source.onerror = () => { state.source.close(); finishRun().catch(() => {}); };
}

$("#prompt-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await execute($("#prompt").value.trim()); }
  catch (error) { alert(error.message); $("#run-button").disabled = false; }
});
$("#prompt").addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.key === "Enter") $("#prompt-form").requestSubmit();
});
$("#copy-answer").addEventListener("click", () => navigator.clipboard.writeText($("#answer").textContent));
$("#model-config-button").addEventListener("click", () => $("#model-dialog").showModal());
$("#close-model-dialog").addEventListener("click", () => $("#model-dialog").close());
$("#model-form").addEventListener("submit", saveProviderConfig);
$("#test-model-config").addEventListener("click", testProviderConfig);
$("#clear-api-key").addEventListener("click", clearApiKey);
$("#reset-model-config").addEventListener("click", resetProviderConfig);
$("#toggle-key").addEventListener("click", () => {
  const key = $("#api-key");
  const visible = key.type === "text";
  key.type = visible ? "password" : "text";
  $("#toggle-key").textContent = visible ? "显示" : "隐藏";
});
bootstrap();
