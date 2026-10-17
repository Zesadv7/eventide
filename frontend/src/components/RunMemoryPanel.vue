<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { memoryApi, type RunMemory } from "../api/memory";
const props = defineProps<{ runId: string; refreshKey?: string | number }>();
const detail = ref<RunMemory | null>(null),
  error = ref(""),
  loading = ref(false);
let version = 0;
async function load() {
  const current = ++version,
    id = props.runId;
  detail.value = null;
  error.value = "";
  if (!id) return;
  loading.value = true;
  try {
    const value = await memoryApi.run(id);
    if (current === version) detail.value = value;
  } catch (e) {
    if (current === version)
      error.value = e instanceof Error ? e.message : "记忆证据读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}
watch(() => [props.runId, props.refreshKey], load, { immediate: true });
onUnmounted(() => version++);
</script>
<template>
  <details v-if="runId" class="run-memory">
    <summary>本次运行的偏好记忆记录</summary>
    <p>
      这里是历史运行证据，不代表当前启用状态。加入请求上下文不保证模型遵循或因此改变答案。
    </p>
    <button :disabled="loading" @click="load">刷新记录</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="detail">
      <p>
        {{
          detail.snapshotted
            ? "本次 Run 已固定偏好快照。"
            : "本次 Run 尚无偏好快照记录。"
        }}
      </p>
      <details v-if="detail.snapshot">
        <summary>运行开始时固定的偏好版本</summary>
        <p v-if="!detail.snapshot.memories.length">快照中没有已选择的偏好。</p>
        <ul>
          <li v-for="memory in detail.snapshot.memories" :key="memory.id">
            {{ memory.key }} · 版本 {{ memory.revision }} ·
            {{ memory.scope === "global" ? "全局（当前安装）" : "项目" }}
          </li>
        </ul>
      </details>
      <p v-if="!detail.injections.length">尚无已加入请求上下文的记录。</p>
      <article
        v-for="(entry, index) in detail.injections"
        :key="entry.attempt_id || index"
      >
        <p>
          已加入请求上下文 · 请求 {{ entry.request_id }} · 尝试
          {{ entry.attempt_id }}
        </p>
        <ul>
          <li v-for="memory in entry.memories" :key="memory.id">
            {{ memory.key }} · 版本 {{ memory.revision }} ·
            {{ memory.scope === "global" ? "全局（当前安装）" : "项目" }}
          </li>
        </ul>
      </article>
      <p v-for="(receipt, index) in detail.receipts" :key="index">
        {{
          receipt.type === "memory.saved"
            ? "偏好已保存，普通新增或编辑下次 Run 生效。"
            : receipt.type === "memory.suggested"
              ? "有待确认的偏好候选，确认前不会启用。"
              : receipt.reason || receipt.type
        }}
      </p>
    </template>
    <RouterLink to="/memory">查看当前记忆设置</RouterLink>
  </details>
</template>
<style scoped>
.run-memory {
  padding: 1rem;
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow-wrap: anywhere;
}
summary {
  cursor: pointer;
}
p,
li {
  font-size: 0.9rem;
}
article {
  border-top: 1px solid var(--line);
}
</style>
