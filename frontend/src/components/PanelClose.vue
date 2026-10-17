<script setup lang="ts">
withDefaults(defineProps<{ label?: string }>(), { label: "关闭详情" });
defineEmits<{ close: [] }>();
</script>
<template>
  <button
    type="button"
    class="panel-close"
    :aria-label="label"
    :title="label"
    @click="$emit('close')"
  >
    <span aria-hidden="true">×</span>
  </button>
</template>
<style scoped>
.panel-close {
  display: block;
  width: 44px;
  height: 44px;
  min-width: 44px;
  min-height: 44px;
  padding: 0;
  margin: 0 0 0 auto;
  justify-self: end;
  align-self: start;
  flex: 0 0 44px;
  font-size: 26px;
  line-height: 1;
}
</style>
