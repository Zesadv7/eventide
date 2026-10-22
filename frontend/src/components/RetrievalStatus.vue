<script setup lang="ts">
import type { RetrievalStatus } from "../api/types";
defineProps<{ state?: RetrievalStatus | null }>();
const labels = {
  preparing: "准备中",
  available: "可用",
  unavailable: "不可用",
  failed: "准备失败",
};
</script>
<template>
  <section class="retrieval-status" aria-label="知识检索状态" role="status">
    <p>知识检索：{{ state ? labels[state.status] : "未记录" }}</p>
    <template v-if="state">
      <p v-if="state.reason">原因：{{ state.reason }}</p>
      <p v-if="state.detail">{{ state.detail }}</p>
      <p v-if="state.workspace_id">实际副本：{{ state.workspace_id }}</p>
      <p v-if="state.location">
        索引位置：{{
          state.location === "managed" ? "服务状态目录" : "旧位置（只读）"
        }}
      </p>
      <p v-if="state.generation != null">索引代次：{{ state.generation }}</p>
      <p v-if="state.model">模型：{{ state.model }}</p>
      <p v-if="state.embedding_fingerprint">
        配置指纹：{{ state.embedding_fingerprint }}
      </p>
      <p class="muted">
        索引可用不代表全部源码最新；命中文件在检索时单独核对。
      </p>
    </template>
  </section>
</template>
<style scoped>
.retrieval-status {
  overflow-wrap: anywhere;
}
.retrieval-status p {
  margin: 0.35rem 0;
}
</style>
