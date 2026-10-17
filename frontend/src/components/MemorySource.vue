<script setup lang="ts">
import type { MemorySource } from "../api/memory";
defineProps<{ source: MemorySource }>();
</script>
<template>
  <div class="memory-source">
    <span
      >来源：{{
        source.type === "manual"
          ? "设置页手动保存"
          : source.type === "explicit"
            ? "用户明确记住指令"
            : "重复偏好候选"
      }}</span
    >
    <RouterLink
      v-if="source.session_id"
      :to="{ name: 'chat', params: { sessionId: source.session_id } }"
      >查看来源会话</RouterLink
    >
    <details v-if="source.event_ids.length">
      <summary>来源事件（{{ source.event_ids.length }}）</summary>
      <ul>
        <li v-for="id in source.event_ids" :key="id">{{ id }}</li>
      </ul>
    </details>
  </div>
</template>
<style scoped>
.memory-source {
  color: var(--muted);
  font-size: 0.85rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  overflow-wrap: anywhere;
}
ul {
  padding-left: 1.2rem;
}
</style>
