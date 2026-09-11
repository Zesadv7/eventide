import {request} from "./transport.js?v=12";
const $ = (selector) => document.querySelector(selector);
let configured = false;
function normalizeBaseUrl(value) {
  const trimmed = (value || "").trim();
  if (!trimmed) return null;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) return trimmed;
  const local = /^(localhost|127\.\d{1,3}(\.\d{1,3}){2}|0\.0\.0\.0|\[::1\])(:\d+)?/i.test(trimmed);
  return `${local ? "http" : "https"}://${trimmed}`;
}

function providerBody() {
  const body = { provider: $("#provider").value, model: $("#model").value.trim(), base_url: normalizeBaseUrl($("#base-url").value) };
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
  configured = config.api_key_configured && !config.configuration_error;
  // The v2 console dropped the old topbar status nodes; the shared dialog is the
  // only required surface now. Missing optional anchors are skipped, never fatal.
  const stateNode = $("#model-state");
  if (stateNode) stateNode.textContent = config.configuration_error ? "配置异常" : configured ? "" : "需要配置";
  const configButton = $("#model-config-button");
  if (configButton) configButton.classList.toggle("configured", configured);
  const providerSelect = $("#provider");
  if (!providerSelect) return;
  const known = [...providerSelect.options].some((option) => option.value === config.provider);
  providerSelect.value = known ? config.provider : "openai_compatible";
  $("#model").value = config.model || "";
  $("#base-url").value = config.base_url || "";
  $("#key-hint").textContent = config.api_key_status === "decrypt_error" ? config.configuration_error : configured ? "安全保存的 API Key 已配置" : "尚未保存 API Key";
}

export async function loadProviderConfig() { renderProviderStatus(await request("/api/config/provider")); }

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
  $("#test-model-config").disabled = true;
  const result = $("#connection-result");
  result.hidden = false;
  result.textContent = "正在检查连接…";
  try {
    const probe = await request("/api/config/provider/test", { method: "POST", body: JSON.stringify(providerBody()) });
    result.textContent = `${probe.message} · ${Math.round(probe.latency_ms)} ms`;
    if (!probe.success && probe.detail) result.textContent += `\n${probe.detail}`;
    result.className = `connection-result ${probe.success ? "success" : "failure"}`;
  } catch (error) { result.textContent = error.message; result.className = "connection-result failure"; }
  finally { $("#test-model-config").disabled = false; }
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
  try { renderProviderStatus(await request("/api/config/provider", { method: "DELETE" })); $("#api-key").value = ""; setConfigMessage("已恢复启动配置", true); }
  catch (error) { setConfigMessage(error.message); }
}


export const isConfigured = () => configured;
export function showConfig(message = "") { $("#model-dialog").showModal(); setConfigMessage(message); }
// The v2 topbar owns the model entry point (#model-button in app.js); the old
// topbar trigger is gone, so optional listeners attach only when nodes exist.
$("#model-config-button")?.addEventListener("click", () => showConfig());
$("#close-model-dialog")?.addEventListener("click", () => $("#model-dialog").close());
$("#model-form")?.addEventListener("submit", saveProviderConfig);
$("#test-model-config")?.addEventListener("click", testProviderConfig);
$("#clear-api-key")?.addEventListener("click", clearApiKey);
$("#reset-model-config")?.addEventListener("click", resetProviderConfig);
$("#toggle-key")?.addEventListener("click", () => {
  const input = $("#api-key");
  input.type = input.type === "password" ? "text" : "password";
  $("#toggle-key").textContent = input.type === "password" ? "显示" : "隐藏";
});
