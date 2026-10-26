<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { inputManifestsApi, type InputManifests } from "../api/inputManifests";
const props = defineProps<{ runId: string }>();
const opened = ref(false),
  loading = ref(false),
  error = ref("");
const data = ref<InputManifests | null>(null);
let generation = 0;
const labels: Record<string, string> = {
  instructions: "系统与规则",
  history: "历史消息",
  tool_catalog: "工具目录",
  skills: "已加载技能结果",
  retrieval: "检索结果",
  tool_results: "其他工具结果",
  envelope: "消息结构",
};
async function load(before?: number) {
  const current = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const result = await inputManifestsApi.list(props.runId, before);
    if (current === generation) data.value = result;
  } catch (e) {
    if (current === generation)
      error.value = e instanceof Error ? e.message : "输入记录读取失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}
function toggle(event: Event) {
  opened.value = (event.target as HTMLDetailsElement).open;
  if (opened.value && !data.value && !loading.value) void load();
}
watch(
  () => props.runId,
  () => {
    generation++;
    data.value = null;
    error.value = "";
    loading.value = false;
    if (opened.value) void load();
  },
);
onUnmounted(() => generation++);
</script>
<template>
  <details class="input-manifests" @toggle="toggle">
    <summary>模型输入记录</summary>
    <p>
      每条对应一次交给模型适配器的输入；不能据此认定供应商已收到或模型已理解。字符数不是
      Token 数，也不是费用。
    </p>
    <button :disabled="loading" @click="load()">刷新最新输入记录</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在读取…</p>
    <template v-if="data">
      <p v-if="!data.entries.length">
        没有已记录的输入清单。旧历史和未开始的调用不会补造记录。
      </p>
      <p>这页 {{ data.entries.length }} 条，事件位置 {{ data.high_water }}。</p>
      <article v-for="entry in data.entries" :key="entry.attempt_id">
        <h4>{{ entry.model }} · {{ entry.kind }}</h4>
        <p>尝试 {{ entry.attempt_id }} · 请求 {{ entry.request_id }}</p>
        <p>
          {{ entry.message_count }} 条消息 · {{ entry.tool_count }} 个工具 ·
          {{ entry.composition.total_characters }} 个序列化字符
        </p>
        <ul>
          <li v-for="(count, key) in entry.composition.components" :key="key">
            {{ labels[key] || key }}：{{ count }}
          </li>
        </ul>
        <p v-if="entry.instructions">
          规则 {{ entry.instructions.source }}（工作区根目录）加载
          {{ entry.instructions.loaded_characters }} 字符，{{
            entry.instructions.truncated
              ? "已截断，后续内容未进入这份规则输入"
              : "本次规则加载未截断"
          }}。
        </p>
        <p v-else>这条记录没有规则来源信息，不能推断规则已加载。</p>
        <details>
          <summary>查看版本和引用</summary>
          <p>系统输入指纹：{{ entry.system_hash }}</p>
          <p>消息指纹：{{ entry.messages_hash }}</p>
          <p>工具目录指纹：{{ entry.tools_hash }}</p>
          <p>工具：{{ entry.tool_names.join("、") || "无" }}</p>
          <p>
            工具结果调用引用：{{ entry.tool_result_ids.join("、") || "无" }}
          </p>
          <p>
            只保存版本和引用，没有在这里复制提示词、源码或工具正文；子目录规则继承尚未实现。
          </p>
        </details>
      </article>
      <button
        v-if="data.next_before !== null"
        :disabled="loading"
        @click="load(data.next_before)"
      >
        较早的输入记录
      </button>
    </template>
  </details>
</template>
<style scoped>
.input-manifests {
  overflow-wrap: anywhere;
}
article {
  border-top: 1px solid var(--border);
  margin-top: 1rem;
}
</style>
