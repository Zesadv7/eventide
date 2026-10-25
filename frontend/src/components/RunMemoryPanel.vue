<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { memoryApi, type RunMemory } from "../api/memory";
const props = defineProps<{ runId: string; refreshKey?: string | number }>();
const detail = ref<RunMemory | null>(null),
  error = ref(""),
  loading = ref(false);
const revoked = ref<Record<string, string>>({});
const revokeErrors = ref<Record<string, string>>({});
const busyId = ref("");
let version = 0;
async function revoke(receipt: RunMemory["receipts"][number]) {
  if (!receipt.memory_id || !receipt.revision) return;
  const id = receipt.memory_id;
  busyId.value = id;
  delete revokeErrors.value[id];
  try {
    await memoryApi.revoke(id, receipt.revision);
    revoked.value = {
      ...revoked.value,
      [id]: "已撤销：后续请求不再注入；历史事件保留。",
    };
    await load();
  } catch (e) {
    revokeErrors.value = {
      ...revokeErrors.value,
      [id]: e instanceof Error ? e.message : "撤销失败，请到记忆设置处理。",
    };
  } finally {
    busyId.value = "";
  }
}
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
  <section v-if="runId">
    <template v-if="detail">
      <article v-for="(receipt, index) in detail!.receipts" :key="index">
        <template
          v-if="
            (receipt.type === 'memory.saved' && receipt.memory_id) ||
            (receipt.type === 'memory.suggested' && receipt.suggestion_id)
          "
        >
          <p>
            {{
              receipt.type === "memory.saved" ? "已记住：" : "记忆候选待确认："
            }}{{ receipt.content }} ·
            {{ receipt.scope === "global" ? "全局" : "项目" }}
            <template v-if="receipt.scope_defaulted">
              · 原话未指定范围，已默认当前项目
            </template>
          </p>
          <p class="receipt-note">
            来源：本次会话中你的原话片段，已绑定出处；确认前候选不会启用。
          </p>
          <template v-if="receipt.type === 'memory.saved' && receipt.memory_id">
            <p v-if="receipt.current_status === 'revoked'" role="status">
              已撤销：历史保存事实保留，后续请求不再注入。
            </p>
            <p v-else-if="revoked[receipt.memory_id]" role="status">
              {{ revoked[receipt.memory_id] }}
            </p>
            <p
              v-else-if="
                receipt.current_revision &&
                receipt.current_revision !== receipt.revision
              "
            >
              这条记忆已修改，请到记忆设置核对当前版本。
            </p>
            <button
              v-else
              :disabled="busyId === receipt.memory_id"
              @click="revoke(receipt)"
            >
              撤销这条记忆
            </button>
            <p v-if="revokeErrors[receipt.memory_id]" role="alert">
              {{ revokeErrors[receipt.memory_id] }}
            </p>
          </template>
        </template>
        <p v-else>{{ receipt.reason || receipt.type }}</p>
      </article>
    </template>
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
          <p v-if="!detail.snapshot.memories.length">
            快照中没有已选择的偏好。
          </p>
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
      </template>
      <RouterLink to="/memory">查看当前记忆设置</RouterLink>
    </details>
  </section>
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
.receipt-note {
  color: var(--muted, #666);
}
</style>
