<script setup lang="ts">
import { useRouter } from "vue-router";
import { trackInternalHistory } from "../composables/internalHistory";
const router = useRouter();
const back = trackInternalHistory(router);
</script>
<template>
  <button type="button" class="page-back" aria-label="返回上一页" @click="back">
    <span aria-hidden="true">‹</span> 返回
  </button>
</template>
<style scoped>
.page-back {
  min-height: 44px;
  min-width: 44px;
  justify-self: start;
  margin-bottom: 12px;
}
</style>
