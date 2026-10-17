<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
defineProps<{ label: string; disabled?: boolean }>();
const open = ref(false),
  root = ref<HTMLElement | null>(null),
  trigger = ref<HTMLButtonElement | null>(null);
function outside(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node)) open.value = false;
}
function escape(event: KeyboardEvent) {
  if (event.key === "Escape" && open.value) {
    open.value = false;
    trigger.value?.focus();
    event.stopPropagation();
  }
}
onMounted(() => document.addEventListener("pointerdown", outside));
onUnmounted(() => document.removeEventListener("pointerdown", outside));
</script>
<template>
  <div ref="root" class="composer-menu" @keydown="escape">
    <button
      ref="trigger"
      class="menu-trigger"
      type="button"
      :disabled="disabled"
      :aria-expanded="open"
      @click="open = !open"
    >
      {{ label
      }}<svg viewBox="0 0 16 16" aria-hidden="true">
        <path
          d="m5 6 3 3 3-3"
          fill="none"
          stroke="currentColor"
          stroke-linecap="round"
        />
      </svg></button
    ><Transition name="popover"
      ><div v-if="open" class="menu-popover">
        <slot :close="() => (open = false)" /></div
    ></Transition>
  </div>
</template>
