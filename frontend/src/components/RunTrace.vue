<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { request } from "../api/client";
import type { components } from "../api/schema";
const props = defineProps<{ runId: string }>();
type Trace = components["schemas"]["TraceReport"];
const report = ref<Trace | null>(null),
  loading = ref(false),
  error = ref("");
let revision = 0,
  mounted = true;
watch(
  () => props.runId,
  () => {
    revision++;
    report.value = null;
    loading.value = false;
    error.value = "";
  },
);
onUnmounted(() => {
  mounted = false;
});
async function load() {
  if (loading.value) return;
  const current = revision,
    identity = props.runId;
  loading.value = true;
  error.value = "";
  try {
    const value = await request<Trace>(
      `/api/runs/${encodeURIComponent(identity)}/trace`,
    );
    if (mounted && current === revision) report.value = value;
  } catch (e) {
    if (mounted && current === revision)
      error.value = e instanceof Error ? e.message : "Trace读取失败";
  } finally {
    if (current === revision) loading.value = false;
  }
}
function duration(span: Trace["spans"][number]) {
  return ((span.end_time_ns - span.start_time_ns) / 1e6).toFixed(1);
}
const labels: Record<string, string> = {
  "eventide.run": "运行",
  "eventide.model": "模型请求",
  "eventide.tool": "工具",
  "eventide.summary": "摘要",
  "eventide.approval": "审批",
};
</script>
<template>
  <section class="trace-panel">
    <h3>脱敏执行 Trace</h3>
    <p class="muted">
      从当前已保存事件生成，包含本次运行和已知子任务。继续运行是关联记录；缺失结果保留未知。导出不含提示正文、工具参数或结果、路径和密钥。
    </p>
    <button :disabled="loading" @click="load">
      {{ loading ? "生成中…" : report ? "刷新 Trace" : "查看 Trace" }}
    </button>
    <a
      v-if="report"
      :href="`/api/runs/${encodeURIComponent(runId)}/trace?download=true`"
      download
      >下载脱敏 Trace JSON</a
    >
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="report">
      {{ report.spans.length }}
      个已观测区间；耗时包含等待，不等同于模型计算耗时。
    </p>
    <ol v-if="report">
      <li v-for="span in report.spans" :key="span.span_id">
        <strong>{{ labels[span.name] || span.name }}</strong> ·
        {{ span.status }} · {{ duration(span) }} ms
        <details>
          <summary>关联与指标</summary>
          <p>
            区间：{{ span.span_id }} · 父区间：{{ span.parent_span_id || "无" }}
          </p>
          <dl>
            <template v-for="(value, key) in span.attributes" :key="key"
              ><dt>{{ key }}</dt>
              <dd>{{ value }}</dd></template
            >
          </dl>
          <p v-if="span.links.length">关联记录：{{ span.links }}</p>
        </details>
      </li>
    </ol>
  </section>
</template>
<style scoped>
.trace-panel {
  border-top: 1px solid var(--border);
  margin-top: 1.2rem;
  padding-top: 0.5rem;
  overflow-wrap: anywhere;
  min-width: 0;
}
a {
  margin-left: 1rem;
}
ol {
  padding-left: 1.2rem;
}
li {
  padding: 0.5rem 0;
}
dl {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 0.3rem;
}
dd {
  margin: 0;
}
</style>
