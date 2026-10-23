<script setup lang="ts">
defineProps<{ disabled?: boolean }>();
const structured = defineModel<boolean>("structured", { required: true });
const scope = defineModel<"code" | "all">("scope", { required: true });
</script>
<template>
  <fieldset :disabled="disabled" class="index-preparation-options">
    <legend>索引准备方式</legend>
    <label><input v-model="structured" type="checkbox" />按函数与类切分</label>
    <label
      >索引范围
      <select v-model="scope" aria-label="索引范围">
        <option value="code">代码调查：跳过明确的生成产物与锁文件</option>
        <option value="all">全部可索引文本：包含生成产物与锁文件</option>
      </select>
    </label>
    <p class="muted">
      结构切分支持
      Python、JS、TS；其他文件使用文本分段。改变切分或范围后，请到知识索引页显式重建；任务不会自动全量重建。
    </p>
  </fieldset>
</template>
<style scoped>
.index-preparation-options {
  display: grid;
  gap: 0.5rem;
  min-width: 0;
}
label {
  display: flex;
  gap: 0.4rem;
  align-items: center;
  flex-wrap: wrap;
}
select {
  max-width: 100%;
}
p {
  margin: 0;
}
</style>
