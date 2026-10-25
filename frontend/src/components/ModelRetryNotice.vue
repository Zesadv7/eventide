<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import type { ModelRetry } from "../api/types";
const props = defineProps<{ retry: ModelRetry }>();
const now = ref(Date.now());
const timer = setInterval(() => {
  now.value = Date.now();
}, 250);
onUnmounted(() => clearInterval(timer));
const seconds = computed(() =>
  Math.max(0, Math.ceil(props.retry.retry_at - now.value / 1000)),
);
const reasons: Record<string, string> = {
  timeout: "模型响应超时",
  network: "模型连接中断",
  rate_limited: "模型服务暂时限流",
  temporary: "模型服务暂时出错",
  stream_truncated: "模型回答未传输完整",
};
</script>
<template>
  <p role="status" aria-live="polite" class="muted">
    {{ reasons[retry.category] || "模型请求暂时失败" }}。
    <template v-if="seconds"
      >{{ seconds }} 秒后自动进行第 {{ retry.next_attempt }} 次尝试。</template
    >
    <template v-else>正在等待第 {{ retry.next_attempt }} 次尝试。</template>
    任务仍在继续；已完成的工具不会因此重新执行。你仍可停止任务。
  </p>
</template>
