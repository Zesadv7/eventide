<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  worktreesApi,
  worktreeRequestState,
  observeWorktrees,
  type WorktreeCatalog,
  type WorktreeDetail,
  type WorktreeInput,
} from "../api/worktrees";
import { useWorkbench } from "../stores/workbench";
const store = useWorkbench(),
  route = useRoute(),
  router = useRouter();
const project = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.project_id || "",
);
const catalog = ref<WorktreeCatalog | null>(null),
  entries = ref<WorktreeDetail[]>([]),
  detail = ref<WorktreeDetail | null>(null);
const name = ref(""),
  gitRef = ref("HEAD"),
  error = ref(""),
  confirmRemove = ref("");
const state = ref(worktreeRequestState(project.value)),
  more = ref(false),
  loadingMore = ref(false);
const locked = computed(() => state.value.active || !!state.value.pending);
const busy = computed(() =>
  entries.value.some((e) => ["running", "queued"].includes(e.status)),
);
const selected = ref("");
let revision = 0,
  catalogFlight = -1,
  historyFlight = -1,
  detailFlight = "",
  mounted = true;
let timer: ReturnType<typeof setInterval> | undefined;
const stop = observeWorktrees(() => {
  state.value = worktreeRequestState(project.value);
});
function message(e: unknown) {
  return e instanceof Error ? e.message : "操作失败";
}
async function loadCatalog() {
  const version = revision,
    owner = project.value;
  if (!owner || catalogFlight === version) return;
  catalogFlight = version;
  try {
    const value = await worktreesApi.catalog(owner);
    if (mounted && version === revision) {
      catalog.value = value;
      confirmRemove.value = "";
    }
  } catch (e) {
    if (mounted && version === revision) error.value = message(e);
  } finally {
    if (catalogFlight === version) catalogFlight = -1;
  }
}
async function loadHistory(older = false) {
  const version = revision,
    owner = project.value;
  if (!owner || historyFlight === version) return;
  historyFlight = version;
  if (older) loadingMore.value = true;
  try {
    const values = await worktreesApi.list(
      owner,
      older ? entries.value.at(-1)?.run_id : undefined,
    );
    if (!mounted || version !== revision) return;
    if (older) {
      const ids = new Set(entries.value.map((e) => e.run_id));
      entries.value.push(...values.filter((e) => !ids.has(e.run_id)));
    } else {
      const ids = new Set(values.map((e) => e.run_id));
      entries.value = [
        ...values,
        ...entries.value.filter((e) => !ids.has(e.run_id)),
      ];
    }
    if (older || entries.value.length <= 20) more.value = values.length === 20;
    if (!selected.value && values[0]) choose(values[0].run_id);
  } catch (e) {
    if (mounted && version === revision) error.value = message(e);
  } finally {
    if (historyFlight === version) {
      historyFlight = -1;
      loadingMore.value = false;
    }
  }
}
function choose(id: string) {
  if (selected.value !== id) {
    selected.value = id;
    detail.value = null;
  }
  if (route.query.operation !== id)
    void router.replace({ query: { operation: id } });
  void loadDetail();
}
async function loadDetail() {
  const version = revision,
    id = selected.value,
    owner = project.value,
    flight = version + ":" + selected.value;
  if (!id || detailFlight === flight) return;
  detailFlight = flight;
  try {
    const value = await worktreesApi.detail(id);
    if (!mounted || version !== revision || id !== selected.value) return;
    if (value.project_id !== owner) throw new Error("该操作属于其他项目。");
    const finished =
      detail.value &&
      ["queued", "running"].includes(detail.value.status) &&
      !["queued", "running"].includes(value.status);
    detail.value = value;
    if (finished) {
      void loadCatalog();
      void loadHistory();
    }
  } catch (e) {
    if (mounted && version === revision && id === selected.value)
      error.value = message(e);
  } finally {
    if (detailFlight === flight) detailFlight = "";
  }
}
async function submit(body: WorktreeInput) {
  const version = revision;
  error.value = "";
  try {
    const receipt = await worktreesApi.create(project.value, body);
    if (!mounted || version !== revision) return;
    choose(receipt.run_id);
    await loadHistory();
    await loadCatalog();
  } catch (e) {
    if (mounted && version === revision) error.value = message(e);
  }
}
function manage(action: WorktreeInput["action"], target: string) {
  if (!catalog.value || locked.value || busy.value) return;
  if (action === "remove" && confirmRemove.value !== target) return;
  void submit({
    action,
    name: target,
    ref: gitRef.value,
    expected_catalog_hash: catalog.value.catalog_hash,
  });
}
async function cancel() {
  const version = revision,
    id = selected.value;
  try {
    await worktreesApi.cancel(id);
    if (mounted && version === revision) {
      await loadDetail();
      await loadHistory();
    }
  } catch (e) {
    if (mounted && version === revision) error.value = message(e);
  }
}
function reset() {
  revision++;
  catalog.value = null;
  entries.value = [];
  detail.value = null;
  name.value = "";
  gitRef.value = "HEAD";
  confirmRemove.value = "";
  error.value = "";
  more.value = false;
  state.value = worktreeRequestState(project.value);
  selected.value =
    typeof route.query.operation === "string" ? route.query.operation : "";
  void loadCatalog();
  void loadHistory();
  void loadDetail();
}
watch(project, reset);
watch(
  () => route.query.operation,
  (id) => {
    if (typeof id === "string" && id !== selected.value) choose(id);
  },
);
onMounted(() => {
  reset();
  timer = setInterval(() => {
    void loadHistory();
    void loadDetail();
  }, 1500);
});
onUnmounted(() => {
  mounted = false;
  if (timer) clearInterval(timer);
  stop();
});
const labels: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};
</script>
<template>
  <section class="page worktrees-page">
    <h2>工作副本管理</h2>
    <p>
      创建独立 Git
      副本，或保留、清理已有副本。删除仅允许没有会话历史、改动、忽略文件和新增提交的副本；Git
      分支继续保留。
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="state.pending">
      上次请求结果尚未确认。<button
        :disabled="state.active"
        @click="submit(state.pending!)"
      >
        重试原副本请求
      </button>
    </p>
    <button :disabled="locked" @click="loadCatalog">刷新副本版本</button>
    <form @submit.prevent="manage('create', name)">
      <label
        >副本名称<input
          v-model="name"
          required
          maxlength="64"
          :disabled="locked || busy"
      /></label>
      <label
        >起始 Git 引用<input
          v-model="gitRef"
          required
          maxlength="256"
          :disabled="locked || busy"
      /></label>
      <button :disabled="locked || busy || !catalog">创建副本</button>
    </form>
    <article v-for="entry in catalog?.worktrees" :key="entry.name" class="copy">
      <h3>{{ entry.name }}</h3>
      <p>{{ entry.path }}</p>
      <p>
        起始提交：{{ entry.base_commit }} ·
        {{ entry.kept ? "已标记保留" : "未标记保留" }}
      </p>
      <p v-if="entry.history_protected">
        此副本有会话记录或正在使用，只能保留。
      </p>
      <button
        :disabled="locked || busy || entry.kept"
        @click="manage('keep', entry.name)"
      >
        标记保留 {{ entry.name }}
      </button>
      <template v-if="!entry.history_protected"
        ><label
          ><input
            type="radio"
            v-model="confirmRemove"
            :value="entry.name"
            :disabled="locked || busy"
          />确认清理 {{ entry.name }}</label
        >
        <button
          :disabled="locked || busy || confirmRemove !== entry.name"
          @click="manage('remove', entry.name)"
        >
          清理副本 {{ entry.name }}
        </button></template
      >
    </article>
    <h3>操作历史</h3>
    <div class="operation-list">
      <button
        v-for="entry in entries"
        :key="entry.run_id"
        @click="choose(entry.run_id)"
      >
        {{ entry.action }} · {{ entry.name }} · {{ labels[entry.status] }}
      </button>
    </div>
    <button v-if="more" :disabled="loadingMore" @click="loadHistory(true)">
      加载更早操作
    </button>
    <article v-if="detail" aria-label="副本操作详情">
      <h3>{{ labels[detail.status] }} · {{ detail.name }}</h3>
      <p>{{ detail.run_id }}</p>
      <p v-if="detail.ref">固定提交：{{ detail.ref }}</p>
      <p v-if="detail.error">{{ detail.error }}</p>
      <p v-if="detail.result">{{ detail.result.path }}</p>
      <button
        v-if="['queued', 'running'].includes(detail.status)"
        @click="cancel"
      >
        停止副本操作
      </button>
      <p>
        重启后未完成操作停驻，不自动重放。停止已派发的 Git
        操作会等待其结果；已完成的创建或清理不会撤销。
      </p>
    </article>
  </section>
</template>
<style scoped>
.worktrees-page {
  max-width: 1000px;
  margin: auto;
  overflow-wrap: anywhere;
}
form,
.operation-list {
  display: grid;
  gap: 0.7rem;
  margin: 1rem 0;
}
form label {
  display: grid;
  gap: 0.4rem;
}
input:not([type="radio"]) {
  width: 100%;
  box-sizing: border-box;
}
button {
  white-space: normal;
  overflow-wrap: anywhere;
}
.copy {
  border-top: 1px solid var(--border);
  margin: 1rem 0;
  padding-top: 1rem;
}
</style>
