<script setup lang="ts">
import ProjectFolderPicker from "../components/ProjectFolderPicker.vue";
import PanelClose from "../components/PanelClose.vue";
import { useUnsavedChanges } from "../composables/useUnsavedChanges";
import { onMounted, ref } from "vue";
import { ApiError, api } from "../api/client";
import { durableMutation } from "../api/mutations";
import { useWorkbench } from "../stores/workbench";
import type { Workspace } from "../api/types";
const store = useWorkbench();
const pickerOpen = ref(false);
const path = ref(""),
  busy = ref(false),
  error = ref(""),
  message = ref("");
useUnsavedChanges(() => !!path.value);
const pendingKey = "eventide.pending-workspace-removal";
const pending = ref(sessionStorage.getItem(pendingKey));
const selected = ref<Workspace | null>(
    pending.value
      ? { workspace_id: pending.value, name: pending.value, path: "" }
      : null,
  ),
  confirmed = ref(false);
async function refresh() {
  await store.loadWorkspaces();
}
onMounted(() => {
  void refresh().catch((reason) => {
    error.value = String(reason);
  });
});
async function add() {
  if (busy.value || !path.value.trim()) return;
  busy.value = true;
  error.value = "";
  message.value = "";
  try {
    const result = await api.addWorkspace(path.value);
    await refresh();
    message.value = "已注册项目：" + result.name;
    path.value = "";
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}
function choose(workspace: Workspace) {
  if (busy.value || pending.value) return;
  selected.value = workspace;
  confirmed.value = false;
  message.value = "";
  error.value = "";
}
async function remove() {
  const target = selected.value;
  if (!target || !confirmed.value || busy.value) return;
  busy.value = true;
  error.value = "";
  message.value = "";
  try {
    pending.value = target.workspace_id;
    sessionStorage.setItem(pendingKey, target.workspace_id);
    await durableMutation(
      "workspace-remove",
      "/api/workspaces/" + encodeURIComponent(target.workspace_id),
      "DELETE",
      {},
    );
    pending.value = null;
    sessionStorage.removeItem(pendingKey);
    const wasSelected = store.selectedWorkspace === target.workspace_id;
    await refresh();
    if (wasSelected) await store.selectWorkspace(store.selectedWorkspace);
    selected.value = null;
    confirmed.value = false;
    message.value = "已移除项目记录，磁盘文件保留。";
  } catch (reason) {
    if (
      reason instanceof ApiError &&
      ([403, 404, 422].includes(reason.status) ||
        reason.mutationOutcome === "rejected")
    ) {
      pending.value = null;
      sessionStorage.removeItem(pendingKey);
    }
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <section class="page">
    <h1>项目管理</h1>
    <p class="muted">
      注册本机目录。移除只注销空项目记录；有会话历史或执行副本的项目不能移除。服务启动项目需保留。
    </p>
    <p v-if="error" role="alert" class="alert error">{{ error }}</p>
    <p v-if="message" role="status" class="alert">{{ message }}</p>
    <button
      v-if="pending && !selected"
      @click="selected = { workspace_id: pending, name: pending, path: '' }"
    >
      继续核对原移除请求
    </button>
    <button type="button" @click="pickerOpen = true">选择项目文件夹</button>
    <ProjectFolderPicker
      v-if="pickerOpen"
      v-model="path"
      :initially-open="true"
      @close="pickerOpen = false"
    />
    <form @submit.prevent="add">
      <label
        >项目目录<input
          v-model="path"
          :disabled="busy"
          required
          placeholder="/path/to/project"
      /></label>
      <button type="submit" :disabled="busy || !path.trim()">注册项目</button>
    </form>
    <article
      v-for="workspace in store.workspaces"
      :key="workspace.workspace_id"
      class="project-record"
    >
      <h2>{{ workspace.name }}</h2>
      <p>{{ workspace.path }}</p>
      <small>{{ workspace.workspace_id }}</small>
      <p v-if="workspace.startup_workspace">服务启动项目</p>
      <button v-else :disabled="busy || !!pending" @click="choose(workspace)">
        选择移除 {{ workspace.name }}
      </button>
    </article>
    <section v-if="selected" class="project-record" aria-label="移除项目确认">
      <PanelClose
        label="关闭项目移除确认"
        @close="
          selected = null;
          confirmed = false;
        "
      />
      <h2>移除 {{ selected.name }}</h2>
      <p>{{ selected.path }}</p>
      <label
        ><input
          v-model="confirmed"
          type="checkbox"
          :disabled="busy"
        />确认只移除项目记录</label
      >
      <button :disabled="busy || !confirmed" @click="remove">移除空项目</button>
      <p class="muted">
        请求结果未知时，刷新后此确认框会保留，重新确认并重试会沿用原身份。
      </p>
    </section>
  </section>
</template>
<style scoped>
.project-record {
  margin-top: 1rem;
  padding: 1rem;
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow-wrap: anywhere;
}
form {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  align-items: end;
}
form label {
  flex: 1;
  min-width: 0;
}
input:not([type="checkbox"]) {
  display: block;
  width: 100%;
  min-width: 0;
}
</style>
