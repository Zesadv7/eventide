<script setup lang="ts">
import type { InputComposition } from "../api/types";
defineProps<{ value?: InputComposition | null }>();
const labels = {
  instructions: "指令",
  history: "对话历史",
  tool_catalog: "工具目录",
  skills: "Skills 内容",
  retrieval: "检索结果",
  tool_results: "其他工具结果",
  envelope: "消息结构",
} as const;
</script>
<template>
  <details v-if="value" class="input-composition">
    <summary>
      输入组成 · {{ value.total_characters.toLocaleString("zh-CN") }} 字符
    </summary>
    <dl class="facts">
      <template v-for="(label, key) in labels" :key="key">
        <dt>{{ label }}</dt>
        <dd>{{ value.components[key].toLocaleString("zh-CN") }} 字符</dd>
      </template>
    </dl>
    <p class="muted">
      这是请求内容与消息结构的字符统计，非 Token 数。Skills
      和检索仅统计当前请求中可识别的工具结果；已压缩的摘要计入对话历史。不能据此分摊实际费用。
    </p>
  </details>
  <small v-else class="muted">输入组成未记录</small>
</template>
