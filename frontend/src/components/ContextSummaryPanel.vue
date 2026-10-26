<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { contextSummariesApi, type SummaryPage } from "../api/contextSummaries";
import SummaryVersionCard from "./SummaryVersionCard.vue";
const props = defineProps<{ sessionId: string }>();
const data = ref<SummaryPage | null>(null),
  opened = ref(false),
  loading = ref(false),
  error = ref("");
const left = ref<number | null>(null),
  right = ref<number | null>(null);
let generation = 0;
async function load(before?: number) {
  const current = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const value = await contextSummariesApi.list(props.sessionId, before);
    if (current === generation) {
      data.value = value;
      left.value = value.versions[0]?.id ?? null;
      right.value = value.versions[1]?.id ?? null;
    }
  } catch (e) {
    if (current === generation)
      error.value = e instanceof Error ? e.message : "摘要版本读取失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}
function toggle(event: Event) {
  opened.value = (event.target as HTMLDetailsElement).open;
  if (opened.value && !data.value && !loading.value) void load();
}
watch(
  () => props.sessionId,
  () => {
    generation++;
    data.value = null;
    left.value = right.value = null;
    error.value = "";
    loading.value = false;
    if (opened.value) void load();
  },
);
onUnmounted(() => generation++);
</script>
<template>
  <details class="context-summaries" @toggle="toggle">
    <summary>摘要与原始证据对照</summary>
    <p>
      选择两个已保存版本并排查看。覆盖范围之后的历史不在这份摘要内；未记录的内容不代表没有发生。
    </p>
    <button :disabled="loading" @click="load()">刷新摘要版本</button>
    <p v-if="loading">正在读取版本…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="data">
      <p v-if="!data.versions.length">
        当前会话没有已保存摘要；不会从旧对话补造摘要。
      </p>
      <div v-else class="summary-comparison">
        <div>
          <label
            >左侧摘要<select v-model="left" aria-label="左侧摘要">
              <option v-for="v in data.versions" :key="v.id" :value="v.id">
                版本 {{ v.id }} · 覆盖至 {{ v.covered_seq }}
              </option>
            </select></label
          >
          <SummaryVersionCard
            v-if="left !== null"
            :session-id="sessionId"
            :checkpoint-id="left"
          />
        </div>
        <div>
          <label
            >右侧摘要<select v-model="right" aria-label="右侧摘要">
              <option :value="null">不对照</option>
              <option v-for="v in data.versions" :key="v.id" :value="v.id">
                版本 {{ v.id }} · 覆盖至 {{ v.covered_seq }}
              </option>
            </select></label
          >
          <SummaryVersionCard
            v-if="right !== null"
            :session-id="sessionId"
            :checkpoint-id="right"
          />
        </div>
      </div>
      <button
        v-if="data.next_before !== null"
        :disabled="loading"
        @click="load(data.next_before)"
      >
        更早的摘要版本
      </button>
    </template>
  </details>
</template>
<style scoped>
.summary-comparison {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.summary-comparison > div {
  min-width: 0;
}
select {
  width: 100%;
  margin-bottom: 8px;
}
@media (max-width: 800px) {
  .summary-comparison {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
