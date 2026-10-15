<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ text: string }>();
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
        <small v-if="block.language">{{ block.language }}</small>
        <pre><code>{{block.text}}</code></pre>
      </div>
      <p v-else>{{ block.text }}</p></template
    >
  </div>
</template>
