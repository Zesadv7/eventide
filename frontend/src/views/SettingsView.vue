<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api } from "../api/client";
import {
  configurationStatus,
  observeConfiguration,
  rejectedConfiguration,
} from "../api/configuration";
import { useWorkbench } from "../stores/workbench";
import type { ProviderInput } from "../api/types";
import {
  hasPendingProbe,
  observeProbe,
  probeApi,
  probeStatus,
  type ProbeResult,
} from "../api/probe";
import {
  useUnsavedChanges,
  confirmDiscard,
} from "../composables/useUnsavedChanges";
import PanelClose from "../components/PanelClose.vue";
import GenerationEditor from "../components/GenerationEditor.vue";
const store = useWorkbench(),
  provider = ref<ProviderInput["provider"]>("openai_responses"),
  model = ref(""),
  baseUrl = ref(""),
  apiKey = ref(""),
  loading = ref(true),
  saving = ref(false),
  probing = ref(false),
  pendingProbe = ref(hasPendingProbe()),
  probeResult = ref<ProbeResult | null>(null),
  probeError = ref(""),
  connectionForm = ref<HTMLFormElement | null>(null),
  message = ref(""),
  error = ref(""),
  generationScope = ref<"model" | "session">("model");
const configStatus = ref(configurationStatus("provider")),
  configHash = ref(""),
  refreshRequired = ref(false);
let mounted = true,
  readRevision = 0;
const stopObservingConfiguration = observeConfiguration(() => {
  const next = configurationStatus("provider");
  if (configStatus.value.active && !next.active && !saving.value)
    refreshRequired.value = true;
  configStatus.value = next;
});
const busy = computed(
  () =>
    loading.value || saving.value || probing.value || configStatus.value.active,
);
const probeHidden = ref(false);
function syncProbe() {
  const status = probeStatus();
  const wasActive = probing.value;
  probing.value = status.active;
  pendingProbe.value = status.pending;
  if (!probeHidden.value) probeResult.value = status.result;
  if (status.active) probeError.value = "";
  if (wasActive && !status.active && loading.value && mounted)
    void refreshConfiguration();
}
const stopObservingProbe = observeProbe(syncProbe);
syncProbe();
onUnmounted(() => {
  mounted = false;
  readRevision++;
  stopObservingProbe();
  stopObservingConfiguration();
});
const generationEditor = ref<InstanceType<typeof GenerationEditor> | null>(
  null,
);
function selectGenerationScope(value: "model" | "session") {
  if (generationEditor.value?.confirmLeave?.() !== false)
    generationScope.value = value;
}
const cleanConnection = ref("");
const connectionSnapshot = () =>
  JSON.stringify([provider.value, model.value, baseUrl.value]);
const connectionDirty = () =>
  !!apiKey.value ||
  (!!cleanConnection.value && connectionSnapshot() !== cleanConnection.value);
useUnsavedChanges(connectionDirty);
function closeProbe() {
  probeHidden.value = true;
}
const form = computed<ProviderInput>(() => ({
  clear_api_key: false,
  provider: provider.value,
  model: model.value.trim(),
  base_url: baseUrl.value.trim() || null,
  ...(apiKey.value.trim() ? { api_key: apiKey.value.trim() } : {}),
}));
async function refreshConfiguration(replace = true) {
  if (configStatus.value.active || probing.value) return false;
  const revision = ++readRevision;
  loading.value = true;
  try {
    const value = await api.provider();
    if (!mounted || revision !== readRevision) return false;
    store.provider = value;
    configHash.value = value.config_hash;
    refreshRequired.value = false;
    if (replace && (!configStatus.value.pending || !model.value)) {
      provider.value = value.provider;
      model.value = value.model;
      baseUrl.value = value.base_url || "";
      cleanConnection.value = connectionSnapshot();
    }
    error.value = "";
    return true;
  } catch {
    if (mounted && revision === readRevision) {
      refreshRequired.value = true;
      error.value = "无法读取当前配置，请点击刷新；未改变待确认请求身份。";
    }
    return false;
  } finally {
    if (mounted && revision === readRevision) loading.value = false;
  }
}
onMounted(() => {
  // An in-flight request survives route changes; read only after its owner settles.
  if (configStatus.value.active) {
    loading.value = false;
    refreshRequired.value = true;
  } else void refreshConfiguration();
});
async function save() {
  if (
    busy.value ||
    pendingProbe.value ||
    configStatus.value.method === "DELETE" ||
    (refreshRequired.value && !configStatus.value.pending)
  )
    return;
  if (generationEditor.value?.confirmLeave?.() === false) return;
  saving.value = true;
  error.value = "";
  message.value = "";
  const candidate = { ...form.value };
  try {
    await api.saveProvider(candidate, configHash.value);
    cleanConnection.value = connectionSnapshot();
    apiKey.value = "";
    const fresh = await refreshConfiguration();
    message.value = fresh
      ? "连接配置保存已确认，已读取服务当前配置。尚未发起模型调用。"
      : "连接配置保存已确认；当前配置读取失败，请刷新后再编辑。";
  } catch (e) {
    if (rejectedConfiguration(e)) refreshRequired.value = true;
    const text = e instanceof Error ? e.message : "保存结果未确认";
    error.value = candidate.api_key
      ? text.split(candidate.api_key).join("[已隐藏]")
      : text;
  } finally {
    saving.value = false;
  }
}
async function reset() {
  if (
    busy.value ||
    pendingProbe.value ||
    configStatus.value.method === "PUT" ||
    (refreshRequired.value && !configStatus.value.pending)
  )
    return;
  if (
    !configStatus.value.pending &&
    !confirm("恢复服务启动时的模型配置？已保存连接将被移除。")
  )
    return;
  if (generationEditor.value?.confirmLeave?.() === false) return;
  saving.value = true;
  error.value = "";
  message.value = "";
  try {
    await api.resetProvider(configHash.value);
    apiKey.value = "";
    const fresh = await refreshConfiguration();
    message.value = fresh
      ? "恢复启动配置已确认，已读取服务当前配置。"
      : "恢复请求已确认；当前配置读取失败，请刷新后再编辑。";
  } catch (e) {
    if (rejectedConfiguration(e)) refreshRequired.value = true;
    error.value = e instanceof Error ? e.message : "恢复结果未确认";
  } finally {
    saving.value = false;
  }
}
async function testConnection() {
  probeHidden.value = false;
  if (
    busy.value ||
    configStatus.value.pending ||
    !connectionForm.value?.reportValidity()
  )
    return;
  const candidate = { ...form.value };
  probing.value = true;
  probeError.value = "";
  probeResult.value = null;
  try {
    probeResult.value = await probeApi.test(candidate);
  } catch (e) {
    const text = e instanceof Error ? e.message : "连接测试结果未确认";
    probeError.value = candidate.api_key
      ? text.split(candidate.api_key).join("[已隐藏]")
      : text;
  } finally {
    pendingProbe.value = hasPendingProbe();
    probing.value = false;
  }
}
</script>
<template>
  <section class="page settings-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">MODEL / CONNECTION</p>
        <h1>模型与生成设置</h1>
        <p class="muted">连接负责接入，生成参数按模型和会话保存。</p>
      </div>
    </header>
    <section class="panel">
      <h2>模型连接</h2>
      <form ref="connectionForm" @submit.prevent="save" class="form-stack">
        <fieldset :disabled="busy" class="connection-fields form-stack">
          <label
            >API 类型<select v-model="provider">
              <option value="openai_responses">OpenAI Responses</option>
              <option value="openai_compatible">
                Chat Completions 兼容接口
              </option>
              <option value="anthropic">Anthropic Messages</option>
            </select></label
          ><label
            >服务地址<input
              v-model="baseUrl"
              type="url"
              placeholder="留空使用官方地址"
              autocomplete="off" /></label
          ><label
            >默认模型<input
              v-model="model"
              required
              maxlength="256"
              placeholder="模型标识"
              autocomplete="off" /></label
          ><label
            >API Key<input
              v-model="apiKey"
              type="password"
              :placeholder="
                store.provider?.api_key_configured
                  ? '已配置，留空保留原密钥'
                  : '输入模型服务密钥'
              "
              autocomplete="new-password"
              spellcheck="false"
          /></label>
        </fieldset>
        <p class="muted">
          密钥状态：{{
            store.provider?.api_key_configured ? "已配置" : "尚未配置"
          }}
          · 来源：{{
            store.provider?.api_key_source || "未知"
          }}。密钥不写入浏览器存储，保存后表单清空。
        </p>
        <p v-if="store.provider?.configuration_error" class="inline-error">
          {{ store.provider.configuration_error }}
        </p>
        <div class="button-row">
          <button
            class="primary"
            :disabled="
              busy ||
              pendingProbe ||
              configStatus.method === 'DELETE' ||
              (refreshRequired && !configStatus.pending) ||
              !model.trim()
            "
          >
            {{
              saving
                ? "保存中…"
                : configStatus.pending && configStatus.method === "PUT"
                  ? "重试原保存请求"
                  : "保存连接"
            }}</button
          ><button
            type="button"
            :disabled="
              busy ||
              pendingProbe ||
              configStatus.method === 'PUT' ||
              (refreshRequired && !configStatus.pending)
            "
            @click="reset"
          >
            {{
              configStatus.pending && configStatus.method === "DELETE"
                ? "重试原恢复请求"
                : "恢复启动配置"
            }}
          </button>
          <button
            type="button"
            :disabled="busy || configStatus.pending || !model.trim()"
            @click="testConnection"
          >
            {{
              probing ? "测试中…" : pendingProbe ? "重试原连接测试" : "测试连接"
            }}
          </button>
        </div>
        <button
          type="button"
          :disabled="busy"
          @click="
            confirmDiscard(connectionDirty()) &&
            generationEditor?.confirmLeave?.() !== false &&
            refreshConfiguration()
          "
        >
          刷新当前配置
        </button>
        <p v-if="configStatus.pending" class="alert" role="status">
          上次配置请求尚未确认。请保留或重新填写原候选参数和密钥，点击重试原请求；只保存请求键、版本和摘要，不保存密钥。读取当前配置不会清除这条待确认请求。
        </p>
        <p v-if="refreshRequired" class="inline-error" role="alert">
          配置需要重新核对。请显式刷新当前配置后再编辑；不会强制覆盖其他标签页的更改。
        </p>
        <p v-if="message" role="status">{{ message }}</p>
        <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
      </form>
      <p class="muted">
        连接测试使用当前填写的候选配置，留空密钥使用已保存密钥；不会保存或切换活动连接。
        每次主动测试最多请求32个输出Token，计入独立Run及服务UTC当日预算，不计入项目预算。
      </p>
      <p v-if="probing" role="status">
        正在等待连接测试结果。30秒触发取消后仍需等待客户端收尾，用量待确认。
      </p>
      <p v-if="pendingProbe" class="alert" role="status">
        上次连接测试结果尚未确认。请保留或重新填写原候选配置和密钥，再点击“重试原连接测试”；
        页面只保存请求身份和摘要，不保存密钥，不会自动重试。
        <a href="#/usage">查看服务每日用量与预算</a>
      </p>
      <p v-if="probeError" class="inline-error" role="alert">
        {{ probeError }}
      </p>
      <section
        v-if="probeResult && !probeHidden"
        class="probe-result"
        aria-label="连接测试结果"
      >
        <PanelClose label="关闭连接测试结果" @close="closeProbe" />
        <h3>{{ probeResult.success ? "连接测试成功" : "连接测试未成功" }}</h3>
        <p>{{ probeResult.message }}</p>
        <p>本次候选：{{ probeResult.provider }} / {{ probeResult.model }}</p>
        <p v-if="probeResult.detail" class="inline-error">
          {{ probeResult.detail }}
        </p>
        <p v-if="probeResult.run_id">
          检查Run：<code>{{ probeResult.run_id }}</code> ·
          <a
            :href="`/api/runs/${encodeURIComponent(probeResult.run_id)}/usage`"
            target="_blank"
            rel="noopener"
            >查看调用账本</a
          >
        </p>
        <p v-else>未建立Run，未派发模型请求。</p>
        <p>
          耗时：{{
            probeResult.latency_ms === null
              ? "未知"
              : `${Math.round(probeResult.latency_ms)} ms`
          }}
          ·
          {{ probeResult.recovered ? "已从持久记录恢复结果" : "本次检查结果" }}
        </p>
        <dl class="stat-grid">
          <div>
            <dt>实际请求次数</dt>
            <dd>{{ probeResult.usage.attempt_count }}</dd>
          </div>
          <div>
            <dt>已报告输入Token</dt>
            <dd>
              {{ probeResult.usage.reported_totals.input_tokens ?? "未报告" }}
            </dd>
          </div>
          <div>
            <dt>已报告输出Token</dt>
            <dd>
              {{ probeResult.usage.reported_totals.output_tokens ?? "未报告" }}
            </dd>
          </div>
          <div>
            <dt>未知用量请求</dt>
            <dd>{{ probeResult.usage.unknown_attempts }}</dd>
          </div>
        </dl>
        <p v-if="!probeResult.usage.complete" class="muted">
          用量统计不完整；未知不计作零，未结算额度继续保留。可在“用量”页查看UTC每日预算和费用。
        </p>
      </section>
    </section>
    <section v-if="store.provider" class="panel">
      <h2>生成参数默认值</h2>
      <p class="muted">
        使用已保存的模型
        {{ store.provider.model }}。修改连接中的模型字段后，请先保存连接。
      </p>
      <div class="segmented">
        <button
          :class="{ selected: generationScope === 'model' }"
          @click="selectGenerationScope('model')"
        >
          模型默认</button
        ><button
          :disabled="!store.selectedSession"
          :class="{ selected: generationScope === 'session' }"
          @click="selectGenerationScope('session')"
        >
          当前会话
        </button>
      </div>
      <GenerationEditor
        ref="generationEditor"
        :model="store.provider.model"
        :scope="generationScope"
        :session="
          generationScope === 'session' ? store.selectedSession : undefined
        "
      />
    </section>
  </section>
</template>
<style scoped>
.connection-fields {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.probe-result {
  border-top: 1px solid var(--line);
  margin-top: 20px;
  padding-top: 16px;
  overflow-wrap: anywhere;
}
</style>
