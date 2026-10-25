<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import { diagnosticsApi, type RunDiagnosticsReport } from "../api/diagnostics";

const props = defineProps<{ runId: string }>();
const report = ref<RunDiagnosticsReport | null>(null),
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
    void load();
  },
);
onMounted(() => void load());
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
    const value = await diagnosticsApi.report(identity);
    if (mounted && current === revision) report.value = value;
  } catch (e) {
    if (mounted && current === revision)
      error.value = e instanceof Error ? e.message : "故障报告生成失败";
  } finally {
    if (current === revision) loading.value = false;
  }
}
const statusLabels: Record<string, string> = {
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  unfinished: "未完成（缺少终态）",
  unknown: "未知",
};
const toolLabels: Record<string, string> = {
  completed: "成功",
  failed: "失败",
  unknown: "结果未知",
  abandoned: "已放弃",
  unresolved: "无结果记录",
};
function time(value: number | null | undefined) {
  if (!value) return "未记录";
  return new Date(value * 1000).toLocaleString();
}
</script>
<template>
  <section class="diagnostics-panel">
    <p class="muted">
      报告只由本次运行已保存的事件事实派生：不运行模型或工具，不包含对话正文、源码或完整工具输出，也不会自动上传或发送。
    </p>
    <button :disabled="loading" @click="load">
      {{ loading ? "生成中…" : report ? "重新生成报告" : "生成报告" }}
    </button>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="loading" class="muted" aria-live="polite">正在生成报告…</p>
    <template v-if="report">
      <p class="mono">
        报告格式版本 {{ report.report_version }} · 事件高水位
        {{ report.high_water }} · 状态：{{
          statusLabels[report.status] || report.status
        }}
      </p>
      <p v-if="!report.facts_available" role="alert" class="inline-error">
        缺少可用事件事实：{{ report.unavailable.join("；") }}
      </p>
      <template v-if="report.facts_available">
        <a
          :href="
            diagnosticsApi.downloadUrl(report.run_id, 'json', report.high_water)
          "
          download
          >下载 JSON 诊断</a
        >
        <a
          :href="
            diagnosticsApi.downloadUrl(
              report.run_id,
              'markdown',
              report.high_water,
            )
          "
          download
          >下载 Markdown 报告</a
        >
        <p v-if="report.truncated" class="muted">
          部分内容因有界展示被截断，完整事实以事件记录为准。
        </p>
        <h3>发生了什么</h3>
        <p>{{ report.summary.what_happened }}</p>
        <h3>停在哪里</h3>
        <p>{{ report.summary.where_stopped || "正常结束，没有停驻点。" }}</p>
        <h3>完成了什么</h3>
        <p>{{ report.summary.completed }}</p>
        <h3>哪些结果未知</h3>
        <ul>
          <li v-for="(item, index) in report.summary.unknowns" :key="index">
            {{ item }}
          </li>
        </ul>
        <h3>已知原因</h3>
        <ul v-if="report.known_causes.length">
          <li
            v-for="cause in report.known_causes"
            :key="cause.code + String(cause.ref?.at)"
          >
            【{{ cause.label }}】{{ cause.detail || "事件没有记录细节" }}
          </li>
        </ul>
        <p v-else class="muted">事件中没有记录可直接解释的原因。</p>
        <h3>待查事项</h3>
        <ul v-if="report.pending.length">
          <li
            v-for="item in report.pending"
            :key="item.code + String(item.ref?.at)"
          >
            【{{ item.label }}】{{ item.detail || "缺少可核对的事实" }}
          </li>
        </ul>
        <p v-else class="muted">没有待查事项。</p>
        <h3>下一步可做什么</h3>
        <ul>
          <li v-for="(step, index) in report.summary.next_steps" :key="index">
            {{ step }}
          </li>
        </ul>
        <h3>工具调用</h3>
        <p v-if="!report.tools.length" class="muted">没有工具调用记录。</p>
        <ul v-else>
          <li v-for="tool in report.tools" :key="tool.call_id">
            {{ tool.name }}（{{ toolLabels[tool.status] || tool.status }}）{{
              tool.detail ? `：${tool.detail}` : ""
            }}
          </li>
        </ul>
        <h3>模型请求</h3>
        <p v-if="!report.attempts.length" class="muted">没有模型请求记录。</p>
        <ul v-else>
          <li v-for="attempt in report.attempts" :key="attempt.attempt_id">
            attempt {{ attempt.attempt ?? "?" }} · {{ attempt.status }} · 用量{{
              attempt.usage_source
            }}
          </li>
        </ul>
        <details>
          <summary>缺失与不可用项</summary>
          <ul v-if="report.unavailable.length">
            <li v-for="(item, index) in report.unavailable" :key="index">
              {{ item }}
            </li>
          </ul>
          <p v-else class="muted">没有已知的缺失事实。</p>
          <p v-for="(note, index) in report.notes" :key="index" class="muted">
            {{ note }}
          </p>
        </details>
        <p class="muted">
          报告绑定本次生成的事件高水位（{{
            report.high_water
          }}）；之后的新事件不会混入当前版本，重新生成才会纳入。
        </p>
      </template>
    </template>
  </section>
</template>
<style scoped>
.diagnostics-panel {
  overflow-wrap: anywhere;
  min-width: 0;
}
.diagnostics-panel a {
  margin-right: 1rem;
}
.diagnostics-panel h3 {
  margin: 0.8rem 0 0.3rem;
}
ul {
  padding-left: 1.2rem;
  margin: 0.3rem 0;
}
</style>
