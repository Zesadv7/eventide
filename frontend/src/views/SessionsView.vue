<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  sessionsApi,
  sessionMutationState,
  observeSessionMutations,
  type SessionIntent,
} from "../api/sessions";
import { api } from "../api/client";
import type { Session } from "../api/types";
import { useWorkbench } from "../stores/workbench";
const store = useWorkbench(),
  route = useRoute(),
  router = useRouter();
const project = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.project_id || "",
);
const entries = ref<Session[]>([]),
  selected = ref<Session | null>(null),
  title = ref(""),
  error = ref(""),
  notice = ref(""),
  confirmDelete = ref(false),
  loading = ref(false);
const state = ref(sessionMutationState(project.value));
const locked = computed(() => state.value.active || !!state.value.pending);
let loadGeneration = 0;
let revision = 0,
  mounted = true;
const unsubscribe = observeSessionMutations(() => {
  state.value = sessionMutationState(project.value);
});
function choose(value: Session) {
  selected.value = { ...value };
  title.value = value.title;
  confirmDelete.value = false;
  void router.replace({ query: { session: value.session_id } });
}
async function load() {
  const current = revision,
    generation = ++loadGeneration,
    owner = project.value;
  if (!owner) return;
  loading.value = true;
  try {
    const values = await sessionsApi.list(owner);
    if (!mounted || current !== revision || generation !== loadGeneration)
      return;
    entries.value = values.filter((s) => !s.managed);
    const id = selected.value?.session_id || route.query.session;
    const found = entries.value.find((s) => s.session_id === id);
    if (found) choose(found);
    else {
      selected.value = null;
      title.value = "";
    }
  } catch (e) {
    if (mounted && current === revision && generation === loadGeneration)
      error.value = e instanceof Error ? e.message : "读取失败";
  } finally {
    if (current === revision && generation === loadGeneration)
      loading.value = false;
  }
}
async function submit(intent: SessionIntent) {
  const current = revision;
  error.value = "";
  notice.value = "";
  try {
    await sessionsApi.mutate(project.value, intent);
    if (mounted && current === revision) {
      notice.value = "会话管理操作已保存。";
      await load();
      if (!mounted || current !== revision) return;
      if (
        intent.action === "delete" &&
        store.selectedSession === intent.session
      ) {
        await store.selectWorkspace(store.selectedWorkspace);
      } else {
        const workspace = store.selectedWorkspace;
        const records = await api.sessions(workspace);
        if (mounted && current === revision)
          store.sessions[workspace] = records;
      }
    }
  } catch (e) {
    if (mounted && current === revision)
      error.value = e instanceof Error ? e.message : "操作失败";
  }
}
function update(values: { title?: string; archived?: boolean }) {
  if (!selected.value?.metadata_hash) return;
  void submit({
    session: selected.value.session_id,
    action: "update",
    body: { ...values, expected_metadata_hash: selected.value.metadata_hash },
  });
}
function remove() {
  if (!confirmDelete.value || !selected.value?.metadata_hash) return;
  void submit({
    session: selected.value.session_id,
    action: "delete",
    body: { expected_metadata_hash: selected.value.metadata_hash },
  });
}
function reset() {
  revision++;
  entries.value = [];
  selected.value = null;
  title.value = "";
  error.value = "";
  notice.value = "";
  confirmDelete.value = false;
  state.value = sessionMutationState(project.value);
  void load();
}
watch(project, reset);
watch(
  () => route.query.session,
  (id) => {
    if (id === selected.value?.session_id) return;
    const found = entries.value.find((entry) => entry.session_id === id);
    if (found) choose(found);
  },
);
onMounted(reset);
onUnmounted(() => {
  mounted = false;
  unsubscribe();
});
</script>
<template>
  <section class="page sessions-page">
    <h2>会话管理</h2>
    <p>
      重命名、归档或删除空会话。归档只从普通对话列表隐藏，后台任务继续运行；已有历史保留，不能删除。
    </p>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="state.pending">
      上次操作尚未确认：{{ state.pending.session }}。<button
        :disabled="state.active"
        @click="submit(state.pending!)"
      >
        重试原会话管理请求
      </button>
    </p>
    <button :disabled="loading || locked" @click="load">刷新会话版本</button>
    <p v-if="!entries.length">{{ loading ? "读取中…" : "暂无普通会话。" }}</p>
    <div class="session-grid">
      <button
        v-for="entry in entries"
        :key="entry.session_id"
        :class="{ selected: entry.session_id === selected?.session_id }"
        @click="choose(entry)"
      >
        {{ entry.title === "New session" ? "新对话" : entry.title }} ·
        {{ entry.archived ? "已归档" : "可见" }}
      </button>
    </div>
    <article v-if="selected">
      <h3>
        {{ selected.title === "New session" ? "新对话" : selected.title }}
      </h3>
      <p>{{ selected.session_id }}</p>
      <form @submit.prevent="update({ title })">
        <label
          >会话名称<input
            v-model="title"
            required
            maxlength="96"
            :disabled="locked" /></label
        ><button :disabled="locked || !selected.metadata_hash">保存名称</button>
      </form>
      <button
        :disabled="locked || !selected.metadata_hash"
        @click="update({ archived: !selected.archived })"
      >
        {{ selected.archived ? "恢复会话" : "归档会话" }}
      </button>
      <RouterLink
        :to="{ path: '/history', query: { session: selected.session_id } }"
        >查看运行历史</RouterLink
      >
      <p class="muted">
        若其他标签页修改了此会话，提交会被拒绝。确认最新状态后点击“刷新会话版本”再编辑。
      </p>
      <template v-if="!selected.latest_run"
        ><label
          ><input
            v-model="confirmDelete"
            type="checkbox"
            :disabled="locked"
          />确认删除这条空会话</label
        ><button :disabled="locked || !confirmDelete" @click="remove">
          删除空会话
        </button></template
      >
    </article>
  </section>
</template>
<style scoped>
.sessions-page {
  max-width: 1000px;
  margin: auto;
  overflow-wrap: anywhere;
}
.session-grid {
  display: grid;
  gap: 0.6rem;
  margin: 1rem 0;
}
button {
  white-space: normal;
  overflow-wrap: anywhere;
}
.selected {
  border-color: var(--accent);
}
article {
  border-top: 1px solid var(--border);
  padding-top: 1rem;
}
form {
  display: grid;
  gap: 0.7rem;
  margin-bottom: 1rem;
}
form label {
  display: grid;
  gap: 0.4rem;
}
input:not([type="checkbox"]) {
  width: 100%;
  box-sizing: border-box;
}
a {
  margin-left: 1rem;
}
</style>
