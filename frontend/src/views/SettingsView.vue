<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api/client";
import { useWorkbench } from "../stores/workbench";
import type { ProviderInput } from "../api/types";
import GenerationEditor from "../components/GenerationEditor.vue";
const store = useWorkbench(),
  provider = ref<ProviderInput["provider"]>("openai_responses"),
  model = ref(""),
  baseUrl = ref(""),
  apiKey = ref(""),
  saving = ref(false),
  message = ref(""),
  error = ref(""),
  generationScope = ref<"model" | "session">("model");
const form = computed<ProviderInput>(() => ({
  clear_api_key: false,
  provider: provider.value,
  model: model.value.trim(),
  base_url: baseUrl.value.trim() || null,
  ...(apiKey.value ? { api_key: apiKey.value } : {}),
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
  }
});
async function save() {
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
  if (!confirm("恢复服务启动时的模型配置？已保存连接将被移除。")) return;
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
      <form @submit.prevent="save" class="form-stack">
        <label
          >API 类型<select v-model="provider">
            <option value="openai_responses">OpenAI Responses</option>
            <option value="openai_compatible">Chat Completions 兼容接口</option>
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
          <button class="primary" :disabled="saving || !model.trim()">
            {{ saving ? "保存中…" : "保存连接" }}</button
          ><button type="button" @click="reset">恢复启动配置</button>
        </div>
        <p v-if="message" role="status">{{ message }}</p>
        <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
      </form>
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
