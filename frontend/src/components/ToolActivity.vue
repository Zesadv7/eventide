<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{
  tools: Record<string, unknown>;
  count?: number;
  active?: boolean;
}>();
const entries = computed(() =>
  Object.entries(props.tools).map(([id, value]) => ({
    id,
    data: (value && typeof value === "object"
      ? value
      : { output: value }) as Record<string, unknown>,
  })),
);
function status(data: Record<string, unknown>) {
  if (data.is_error || data.status === "failed") return "执行失败";
  if (data.status === "completed") return "已完成";
  if (data.status === "unknown" || data.status === "outcome_unknown")
    return "结果待确认";
  return String(data.status || "已记录");
}
</script>
<template>
  <details v-if="entries.length || count" class="tool-activity">
    <summary>
      <span
        class="activity-glyph"
        :class="{ 'is-working': active }"
        aria-hidden="true"
        >✳</span
      >工具活动 <span>{{ count ?? entries.length }} 次调用</span
      ><span class="activity-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="tool-activity-list">
      <details v-for="entry in entries" :key="entry.id" class="tool-entry">
        <summary>
          <span>{{ entry.data.name || entry.data.tool_name || entry.id }}</span
          ><small>{{ status(entry.data) }}</small>
        </summary>
        <pre>{{ JSON.stringify(entry.data, null, 2) }}</pre>
      </details>
      <p v-if="!entries.length" class="muted">
        工具明细尚未返回，可在运行证据中查看事件记录。
      </p>
    </div>
  </details>
</template>
