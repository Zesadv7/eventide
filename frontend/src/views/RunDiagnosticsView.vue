<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import RunDiagnostics from "../components/RunDiagnostics.vue";

const route = useRoute();
const runId = computed(() => String(route.params.runId ?? ""));
</script>
<template>
  <section class="diagnostics-page">
    <h2 class="page-heading">运行故障报告</h2>
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
