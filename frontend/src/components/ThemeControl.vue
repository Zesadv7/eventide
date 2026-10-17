<script setup lang="ts">
import { ref, watch } from "vue";
type Theme = "system" | "light" | "dark";
function saved(): Theme {
  try {
    const value = localStorage.getItem("eventide.theme");
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}
const theme = ref<Theme>(saved());
watch(
  theme,
  (value) => {
    document.documentElement.dataset.theme = value;
    try {
      localStorage.setItem("eventide.theme", value);
    } catch {
      /* Storage can be unavailable in private browsing. */
    }
  },
  { immediate: true },
);
</script>
<template>
  <label class="theme-control"
    ><span>外观</span
    ><select v-model="theme" aria-label="外观主题">
      <option value="system">跟随系统</option>
      <option value="light">浅色</option>
      <option value="dark">深色</option>
    </select></label
  >
</template>
