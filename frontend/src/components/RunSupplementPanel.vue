<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import {
  interactionsApi,
  type SupplementProjection,
  type SupplementStatus,
} from "../api/interactions";
const props = defineProps<{
  runId: string;
  runStatus?: string;
  refreshKey?: string | number;
}>();
const detail = ref<SupplementProjection | null>(null),
  draft = ref(""),
  error = ref(""),
  notice = ref(""),
  sending = ref(false),
  loading = ref(false);
let version = 0,
  scope = 0;
async function load() {
  const current = ++version,
    id = props.runId;
  if (!id) return;
  loading.value = true;
  try {
    const value = await interactionsApi.list(id);
    if (current === version) detail.value = value;
  } catch (e) {
    if (current === version)
      error.value = e instanceof Error ? e.message : "补充记录读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}
async function send() {
  const id = props.runId,
    currentScope = scope,
    body = draft.value.trim();
  if (!id || !body || sending.value) return;
  sending.value = true;
  error.value = "";
  notice.value = "";
  try {
    const receipt = await interactionsApi.add(id, body);
    if (currentScope !== scope) return;
    if (receipt.status === "late") {
      notice.value =
        "该运行已结束：补充已保存为记录，但不会自动恢复或重开任何运行；是否带入下一轮由你决定。";
    } else {
      notice.value = "已接收；将在下一个模型边界加入当前运行，只消费一次。";
    }
    draft.value = "";
    await load();
  } catch (e) {
    if (currentScope === scope)
      error.value = e instanceof Error ? e.message : "补充发送失败";
  } finally {
    if (currentScope === scope) sending.value = false;
  }
}
const statusLabel: Record<SupplementStatus, string> = {
  pending: "等待下一模型边界",
  consumed: "已消费一次",
  late: "迟到 · 不会自动续跑",
};
watch(
  () => props.runId,
  () => {
    scope++;
    version++;
    detail.value = null;
    draft.value = "";
    notice.value = "";
    error.value = "";
    sending.value = false;
    void load();
  },
  { immediate: true },
);
watch(() => props.refreshKey, load);
onUnmounted(() => {
  version++;
  scope++;
});
</script>
<template>
  <details v-if="runId" class="run-supplements">
    <summary>
      运行中补充
      <span v-if="detail?.pending" class="badge"
        >{{ detail.pending }} 条待消费</span
      >
    </summary>
    <p>
      补充只在当前运行的下一个模型边界加入一次；已执行的工具不会倒带，迟到补充不会自动续跑。
    </p>
    <label class="supplement-editor">
      <textarea
        v-model="draft"
        rows="3"
        maxlength="8000"
        aria-label="运行中补充内容"
        placeholder="给正在运行的任务补充信息（可选）"
      ></textarea>
    </label>
    <div class="button-row">
      <button
        class="primary"
        :disabled="sending || !draft.trim()"
        @click="send"
      >
        {{ sending ? "发送中…" : "发送补充" }}
      </button>
      <button :disabled="loading" @click="load">刷新记录</button>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="detail && !detail.supplements.length" class="muted">
      本次运行暂无补充记录。
    </p>
    <article
      v-for="item in detail?.supplements || []"
      :key="item.supplement_id"
    >
      <p>
        <span class="badge">{{ statusLabel[item.status] }}</span>
        <span v-if="item.status === 'consumed'" class="muted">
          第 {{ item.consumed_step ?? "?" }} 步边界消费
        </span>
        <span class="muted">正文版本 {{ item.body_version }}</span>
      </p>
      <p class="supplement-body">{{ item.body }}</p>
    </article>
  </details>
</template>
<style scoped>
.supplement-editor textarea {
  width: 100%;
}
.supplement-body {
  white-space: pre-wrap;
}
</style>
