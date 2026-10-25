<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import type { ConnectionStatus } from "../api/events";
const props = defineProps<{ status: ConnectionStatus }>();
const now = ref(Date.now());
const timer = setInterval(() => {
  now.value = Date.now();
}, 250);
onUnmounted(() => clearInterval(timer));
const seconds = computed(() =>
  Math.max(
    0,
    Math.ceil(((props.status.nextRetryAt ?? now.value) - now.value) / 1000),
  ),
);
</script>

<template>
  <p class="notice" role="status" aria-live="polite">
    <template v-if="status.state === 'offline'"
      >网络已离线，联网后会自动接回任务进度。</template
    >
    <template v-else-if="status.state === 'retrying'"
      >进度连接中断，{{ seconds }} 秒后第
      {{ status.attempt }} 次重连。</template
    >
    <template v-else>正在连接任务进度…</template>
    已接收的任务不会因页面断线而重新提交。
  </p>
</template>
