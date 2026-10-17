<script setup lang="ts">
import { computed, ref } from "vue";
const props = defineProps<{ text: string }>();
const copied = ref<number | null>(null),
  copyError = ref("");
async function copy(text: string, index: number) {
  try {
    await navigator.clipboard.writeText(text);
    copied.value = index;
    copyError.value = "";
  } catch {
    copyError.value = "无法访问剪贴板，请选择代码后复制。";
  }
}
// Deliberately small Markdown subset. Every user-controlled string remains text;
// raw HTML and arbitrary URL schemes never reach v-html or an href.
const blocks = computed(() =>
  props.text
    .split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g)
    .filter(Boolean)
    .map((part) => {
      if (part.startsWith("```")) {
        const newline = part.indexOf("\n");
        return {
          kind: "code",
          language: part.slice(3, newline).trim(),
          text: part.slice(newline + 1).replace(/```$/, ""),
        };
      }
      return { kind: "text", language: "", text: part };
    }),
);
</script>
<template>
  <div class="safe-content">
    <template v-for="(block, index) in blocks" :key="index"
      ><div v-if="block.kind === 'code'" class="code-block">
        <div class="code-heading">
          <small>{{ block.language || "代码" }}</small
          ><button type="button" @click="copy(block.text, index)">
            {{ copied === index ? "已复制" : "复制代码" }}
          </button>
        </div>
        <pre><code>{{block.text}}</code></pre>
      </div>
      <p v-else>{{ block.text }}</p></template
    >
    <span v-if="copyError" role="status" class="muted">{{ copyError }}</span>
  </div>
</template>
