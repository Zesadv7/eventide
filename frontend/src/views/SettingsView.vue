<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api } from "../api/client";
import { useWorkbench } from "../stores/workbench";
import type { ProviderInput } from "../api/types";
import {
  hasPendingProbe,
  observeProbe,
  probeApi,
  probeStatus,
  type ProbeResult,
} from "../api/probe";
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
const busy = computed(() => loading.value || saving.value || probing.value);
function syncProbe() {
  const status = probeStatus();
  probing.value = status.active;
  pendingProbe.value = status.pending;
  probeResult.value = status.result;
  if (status.active) probeError.value = "";
}
const stopObservingProbe = observeProbe(syncProbe);
syncProbe();
onUnmounted(stopObservingProbe);
const form = computed<ProviderInput>(() => ({
  clear_api_key: false,
  provider: provider.value,
  model: model.value.trim(),
  base_url: baseUrl.value.trim() || null,
  ...(apiKey.value.trim() ? { api_key: apiKey.value.trim() } : {}),
}));
onMounted(async () => {
  try {
    const value = await api.provider();
    store.provider = value;
    provider.value = value.provider;
    model.value = value.model;
    baseUrl.value = value.base_url || "";
  } catch {
    error.value = "无法读取模型配置，请检查本机服务。";
  } finally {
    loading.value = false;
  }
});
async function save() {
  if (busy.value || pendingProbe.value) return;
  saving.value = true;
  error.value = "";
  message.value = "";
  try {
    store.provider = await api.saveProvider(form.value);
    message.value = "连接配置已加密保存。尚未发起模型调用。";
  } catch (e) {
    const text = e instanceof Error ? e.message : "保存失败";
    error.value = apiKey.value
      ? text.split(apiKey.value).join("[已隐藏]")
      : text;
  } finally {
    apiKey.value = "";
    saving.value = false;
  }
}
async function reset() {
  if (busy.value || pendingProbe.value) return;
  if (!confirm("恢复服务启动时的模型配置？已保存连接将被移除。")) return;
  saving.value = true;
  try {
    const value = await api.resetProvider();
    store.provider = value;
    provider.value = value.provider;
    model.value = value.model;
    baseUrl.value = value.base_url || "";
    apiKey.value = "";
    message.value = "已恢复启动配置。";
  } catch (e) {
    error.value = e instanceof Error ? e.message : "恢复失败";
  } finally {
    saving.value = false;
  }
}
async function testConnection() {
  if (busy.value || !connectionForm.value?.reportValidity()) return;
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
            :disabled="busy || pendingProbe || !model.trim()"
          >
            {{ saving ? "保存中…" : "保存连接" }}</button
          ><button
            type="button"
            :disabled="busy || pendingProbe"
            @click="reset"
          >
            恢复启动配置
          </button>
          <button
            type="button"
            :disabled="busy || !model.trim()"
            @click="testConnection"
          >
            {{
              probing ? "测试中…" : pendingProbe ? "重试原连接测试" : "测试连接"
            }}
          </button>
        </div>
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
        v-if="probeResult"
        class="probe-result"
        aria-label="连接测试结果"
      >
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
          @click="generationScope = 'model'"
        >
          模型默认</button
        ><button
          :disabled="!store.selectedSession"
          :class="{ selected: generationScope === 'session' }"
          @click="generationScope = 'session'"
        >
          当前会话
        </button>
      </div>
      <GenerationEditor
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
