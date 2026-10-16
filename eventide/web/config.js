import {request} from "./transport.js?v=12";
import {testConnection, hasPendingConnection} from "./probe.js?v=2";
import {mutateConfiguration, configurationStatus, rejectedConfiguration} from "./configuration.js?v=1";
const $ = (selector) => document.querySelector(selector);
let configured = false;
let configHash = "";
let busy = false;
let refreshRequired = false;
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
  configHash = config.config_hash;
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
  window.dispatchEvent(new CustomEvent("eventide:provider-config", {detail: config}));
}

export async function loadProviderConfig() {
  const status = await request("/api/config/provider");
  renderProviderStatus(status);
  return status;
}

function lockConfiguration(value) {
  busy = value;
  for (const node of $("#model-form").querySelectorAll("input, select, button")) node.disabled = value;
  for (const id of ["#clear-api-key", "#reset-model-config", "#test-model-config"]) {
    const node = $(id); if (node) node.disabled = value;
  }
}
async function changeConfiguration(operation, body) {
  if (busy || hasPendingConnection()) return;
  if (refreshRequired && !configurationStatus().pending) {
    await loadProviderConfig(); refreshRequired = false;
    setConfigMessage("已刷新当前配置，请检查后重新填写并保存。"); return;
  }
  lockConfiguration(true);
  try {
    await mutateConfiguration(operation, body, configHash);
    $("#api-key").value = "";
    await loadProviderConfig();
    setConfigMessage("配置操作已确认，已读取当前配置", true);
  } catch (error) {
    if (rejectedConfiguration(error)) refreshRequired = true;
    setConfigMessage(error.message + (configurationStatus().pending ? "；结果未确认，请恢复原候选参数和密钥后重试同一操作。" : ""));
  } finally { lockConfiguration(false); }
}
async function saveProviderConfig(event) {
  event.preventDefault();
  await changeConfiguration("save", providerBody());
}

async function testProviderConfig() {
  if (busy || configurationStatus().pending || !$("#model-form").reportValidity()) return;
  lockConfiguration(true);
  const result = $("#connection-result");
  result.hidden = false;
  result.textContent = "正在检查连接…";
  try {
    const probe = await testConnection(providerBody());
    result.textContent = probe.message;
    if (probe.latency_ms !== null) result.textContent += ` · ${Math.round(probe.latency_ms)} ms`;
    if (probe.run_id) result.textContent += `\n检查记录：${probe.run_id}`;
    if (!probe.success && probe.detail) result.textContent += `\n${probe.detail}`;
    result.className = `connection-result ${probe.success ? "success" : "failure"}`;
  } catch (error) {
    result.textContent = error.message + (error.runId ? `\n检查记录：${error.runId}` : "");
    result.className = "connection-result failure";
  }
  finally { lockConfiguration(false); }
}

async function clearApiKey() {
  if (!configurationStatus().pending && !confirm("确定清除已保存的 API Key 吗？")) return;
  const body = {...providerBody(), clear_api_key: true};
  delete body.api_key;
  await changeConfiguration("clear", body);
}
async function resetProviderConfig() {
  if (!configurationStatus().pending && !confirm("确定恢复服务启动时的配置吗？")) return;
  await changeConfiguration("reset", {});
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
