<script setup lang="ts">
import { ref, watch } from "vue";
import { ApiError } from "../api/client";
import {
  fileOperationsApi,
  type FileOperation,
  type FileOperationDetail,
} from "../api/fileOperations";
import DiffViewer from "./DiffViewer.vue";
const props = defineProps<{ sessionId: string; revision?: unknown }>();
const operations = ref<FileOperation[]>([]);
const selected = ref<FileOperationDetail | null>(null);
const opened = ref(false);
const loaded = ref(false);
const loading = ref(false);
const detailLoading = ref(false);
const undoing = ref(false);
const error = ref("");
const notice = ref("");
let listVersion = 0;
let detailVersion = 0;
const labels: Record<FileOperation["status"], string> = {
  prepared: "写入尚未确认",
  completed: "已完成",
  no_change: "无变化",
  failed: "写入失败",
  needs_review: "待核验，禁止撤销",
  undo_prepared: "撤销尚未确认，待核验",
  undone: "已撤销",
};
const kinds = { added: "新增文件", modified: "修改文件", unchanged: "无变化" };
watch(
  () => props.sessionId,
  () => {
    listVersion++;
    detailVersion++;
    operations.value = [];
    selected.value = null;
    loaded.value = false;
    loading.value = false;
    detailLoading.value = false;
    error.value = "";
    notice.value = "";
    if (opened.value) void refresh();
  },
);
watch(
  () => props.revision,
  () => {
    if (opened.value && loaded.value && !undoing.value) void refresh();
  },
);
async function refresh() {
  const session = props.sessionId;
  const version = ++listVersion;
  loading.value = true;
  try {
    const result = await fileOperationsApi.list(session);
    if (version !== listVersion || session !== props.sessionId) return;
    operations.value = result.operations;
    loaded.value = true;
    if (selected.value) {
      const current = operations.value.find(
        (item) => item.operation_id === selected.value!.operation_id,
      );
      if (current) selected.value = { ...selected.value, ...current };
    }
  } catch (reason) {
    if (version === listVersion)
      error.value =
        reason instanceof Error ? reason.message : "文件操作读取失败";
  } finally {
    if (version === listVersion) loading.value = false;
  }
}
function toggle(event: Event) {
  opened.value = (event.currentTarget as HTMLDetailsElement).open;
  if (opened.value && !loaded.value) void refresh();
}
async function inspect(operation: FileOperation) {
  const session = props.sessionId;
  const version = ++detailVersion;
  selected.value = null;
  detailLoading.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await fileOperationsApi.detail(
      session,
      operation.operation_id,
    );
    if (version === detailVersion && session === props.sessionId)
      selected.value = result;
  } catch (reason) {
    if (version === detailVersion)
      error.value =
        reason instanceof Error ? reason.message : "修改对比读取失败";
  } finally {
    if (version === detailVersion) detailLoading.value = false;
  }
}
async function undo() {
  const operation = selected.value;
  if (!operation || operation.status !== "completed" || undoing.value) return;
  const session = props.sessionId;
  undoing.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await fileOperationsApi.undo(
      session,
      operation.operation_id,
    );
    if (session !== props.sessionId) return;
    if (selected.value?.operation_id === operation.operation_id)
      selected.value = { ...operation, ...result };
    operations.value = operations.value.map((item) =>
      item.operation_id === result.operation_id ? result : item,
    );
    notice.value =
      result.status === "undone"
        ? "本次修改已撤销。"
        : "请核对服务返回的操作状态。";
  } catch (reason) {
    if (session !== props.sessionId) return;
    error.value =
      reason instanceof ApiError && reason.status === 409
        ? `撤销冲突或状态不允许：未覆盖当前文件，请重新核对。${reason.message}`
        : reason instanceof Error
          ? reason.message
          : "撤销未确认，请刷新操作状态后判断";
    await refresh();
  } finally {
    undoing.value = false;
  }
}
</script>
<template>
  <details class="file-operations" @toggle="toggle">
    <summary>文件修改与撤销（本会话）</summary>
    <p class="muted">
      对比来自每次内置 write/edit
      的实际前后内容。仅撤销所选文件的这一次修改；Shell 和交付采纳不在此范围。
    </p>
    <button :disabled="loading || undoing" @click="refresh">
      刷新操作状态
    </button>
    <p v-if="loading || detailLoading" role="status">正在读取…</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loaded && !operations.length">尚无受控内置文件操作记录。</p>
    <ul class="operation-list">
      <li v-for="operation in operations" :key="operation.operation_id">
        <button :disabled="undoing" @click="inspect(operation)">
          {{ operation.path }} · {{ kinds[operation.change_kind] }} ·
          {{ labels[operation.status] }}
        </button>
        <small>Run {{ operation.run_id }} · 调用 {{ operation.call_id }}</small>
      </li>
    </ul>
    <section v-if="selected" aria-label="单次文件操作详情">
      <h4>{{ selected.path }} · {{ kinds[selected.change_kind] }}</h4>
      <p>{{ labels[selected.status] }} · 操作 {{ selected.operation_id }}</p>
      <p>实际工作区：{{ selected.workspace_id }}</p>
      <DiffViewer
        v-if="selected.diff_status === 'available'"
        :patch="selected.patch || ''"
      />
      <p v-else>
        {{ selected.diff_message || "此操作无法展示文本差异。" }}
      </p>
      <button
        v-if="selected.status === 'completed'"
        :disabled="undoing"
        @click="undo"
      >
        {{ undoing ? "等待撤销结果…" : "撤销本次修改" }}
      </button>
      <p v-if="selected.status === 'completed'" class="muted">
        撤销会重新核对当前内容；后续修改发生冲突时会拒绝覆盖。
      </p>
    </section>
  </details>
</template>
<style scoped>
.file-operations {
  margin-top: 0.8rem;
  overflow-wrap: anywhere;
}
.operation-list {
  padding-left: 1.2rem;
}
.operation-list li {
  margin: 0.5rem 0;
}
.operation-list small {
  display: block;
  opacity: 0.7;
}
h4 {
  margin-bottom: 0.4rem;
}
</style>
