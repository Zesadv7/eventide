<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import RecoveryPanel from "../components/RecoveryPanel.vue";

const route = useRoute();
const runId = computed(() => String(route.params.runId ?? ""));
</script>
<template>
  <section class="recovery-page">
    <h2 class="page-heading">运行恢复详情</h2>
    <RecoveryPanel v-if="runId" :run-id="runId" />
    <p v-else class="muted" role="alert">缺少 Run 身份，无法读取恢复详情。</p>
  </section>
</template>
<style scoped>
.recovery-page {
  overflow-wrap: anywhere;
  min-width: 0;
}
</style>
