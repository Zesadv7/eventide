<script setup lang="ts">
import { ref, watch, onUnmounted } from "vue";
import { request } from "../api/client";
const props = defineProps<{ session: string }>();
interface Message {
  seq: number;
  role: string;
  content: unknown;
}
const entries = ref<Message[]>([]),
  loading = ref(false),
  more = ref(false),
  error = ref("");
let version = 0,
  mounted = true;
async function load(older = false) {
  if (loading.value) return;
  const current = version,
    owner = props.session;
  loading.value = true;
  error.value = "";
  try {
    const suffix =
      older && entries.value[0] ? "&before=" + entries.value[0].seq : "";
    const values = await request<Message[]>(
      "/api/sessions/" +
        encodeURIComponent(owner) +
        "/legacy-messages?limit=50" +
        suffix,
    );
    if (!mounted || current !== version) return;
    entries.value = older ? [...values, ...entries.value] : values;
    more.value = values.length === 50;
  } catch (e) {
    if (mounted && current === version)
      error.value = e instanceof Error ? e.message : "读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}
watch(
  () => props.session,
  () => {
    version++;
    entries.value = [];
    loading.value = false;
    more.value = false;
    void load();
  },
  { immediate: true },
);
onUnmounted(() => {
  mounted = false;
});
</script>
<template>
  <section class="legacy-messages" aria-label="旧版只读消息">
    <h3>旧版只读消息</h3>
    <p>这些内容是导入的历史文本，不会作为新的工具调用或执行指令。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="more" :disabled="loading" @click="load(true)">
      加载更早旧消息
    </button>
    <p v-if="loading">读取中…</p>
    <article v-for="entry in entries" :key="entry.seq">
      <strong>{{ entry.role }}</strong>
      <pre>{{
        typeof entry.content === "string"
          ? entry.content
          : JSON.stringify(entry.content, null, 2)
      }}</pre>
    </article>
  </section>
</template>
<style scoped>
.legacy-messages {
  border: 1px solid var(--border);
  padding: 1rem;
  margin: 1rem 0;
  overflow-wrap: anywhere;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 25rem;
  overflow: auto;
}
</style>
