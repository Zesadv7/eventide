<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";
import RunDiagnostics from "../components/RunDiagnostics.vue";

const route = useRoute();
const runId = computed(() => String(route.params.runId ?? ""));
</script>
<template>
  <section class="diagnostics-page">
    <h2 class="page-heading">运行故障报告</h2>
    <p v-if="runId" class="muted">
      <RouterLink :to="{ name: 'run-recovery', params: { runId } }">
        查看当前恢复状态
      </RouterLink>
      · 当前状态可能晚于这份报告；可用操作由恢复页重新核对。
    </p>
    <RunDiagnostics v-if="runId" :run-id="runId" />
    <p v-else class="muted" role="alert">缺少 Run 身份，无法生成报告。</p>
  </section>
</template>
<style scoped>
.diagnostics-page {
  overflow-wrap: anywhere;
  min-width: 0;
}
</style>
