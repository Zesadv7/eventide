<script setup lang="ts">
import { useUnsavedChanges } from "../composables/useUnsavedChanges";
import PanelClose from "../components/PanelClose.vue";
const detailClosed = ref(false);
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  importsApi,
  importState,
  observeImports,
  type ImportDetail,
  type ImportIntent,
} from "../api/historyImports";
import { useWorkbench } from "../stores/workbench";
const store = useWorkbench(),
  route = useRoute(),
  router = useRouter();
const project = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.project_id || "",
);
const entries = ref<ImportDetail[]>([]),
  detail = ref<ImportDetail | null>(null),
  source = ref("");
const error = ref(""),
  selected = ref(""),
  confirmed = ref(false),
  more = ref(false),
  loadingMore = ref(false);
const cleanSource = ref("");
useUnsavedChanges(() => source.value !== cleanSource.value);
const state = ref(importState(project.value));
const locked = computed(() => state.value.active || !!state.value.pending);
const busy = computed(() =>
  entries.value.some((e) => ["queued", "running"].includes(e.status)),
);
let mounted = true,
  revision = 0,
  listFlight = -1,
  detailFlight = "";
let timer: ReturnType<typeof setInterval> | undefined;
const stop = observeImports(() => {
  state.value = importState(project.value);
});
function message(e: unknown) {
  return e instanceof Error ? e.message : "请求失败";
}
function choose(id: string) {
  detailClosed.value = !id;
  if (selected.value !== id) {
    selected.value = id;
    detail.value = null;
    confirmed.value = false;
  }
  if (route.query.operation !== id)
    void router.replace({ query: { operation: id } });
  void loadDetail();
}
async function loadList(older = false) {
  const version = revision,
    owner = project.value;
  if (!owner || listFlight === version) return;
  listFlight = version;
  if (older) loadingMore.value = true;
  try {
    const values = await importsApi.list(
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
    if (!detailClosed.value && !selected.value && values[0])
      choose(values[0].run_id);
  } catch (e) {
    if (mounted && version === revision) error.value = message(e);
  } finally {
    if (listFlight === version) {
      listFlight = -1;
      loadingMore.value = false;
    }
  }
}
async function loadDetail() {
  const version = revision,
    owner = project.value,
    id = selected.value,
    flight = version + ":" + selected.value;
  if (!id || detailFlight === flight) return;
  detailFlight = flight;
  try {
    const value = await importsApi.detail(id);
    if (!mounted || version !== revision || selected.value !== id) return;
    if (value.project_id !== owner) throw new Error("该导入记录属于其他项目。");
    if (detail.value?.preview?.sha256 !== value.preview?.sha256)
      confirmed.value = false;
    detail.value = value;
  } catch (e) {
    if (mounted && version === revision && selected.value === id)
      error.value = message(e);
  } finally {
    if (detailFlight === flight) detailFlight = "";
  }
}
async function submit(intent: ImportIntent) {
  const version = revision,
    savedSource = source.value;
  error.value = "";
  try {
    const receipt = await importsApi.submit(project.value, intent);
    if (!mounted || version !== revision) return;
    if (intent.action === "preview") cleanSource.value = savedSource;
    if (!detailClosed.value) choose(receipt.run_id);
    await loadList();
  } catch (e) {
    if (mounted && version === revision) error.value = message(e);
  }
}
function preview() {
  if (!project.value || locked.value || busy.value || !source.value.trim())
    return;
  void submit({ action: "preview", body: { source: source.value } });
}
function apply() {
  if (
    !confirmed.value ||
    !detail.value?.preview ||
    detail.value.imported ||
    locked.value ||
    busy.value
  )
    return;
  void submit({
    action: "import",
    body: {
      preview_id: detail.value.run_id,
      archive_sha256: detail.value.preview.sha256,
      confirmed: true,
    },
  });
}
async function cancel() {
  const version = revision,
    id = selected.value;
  try {
    await importsApi.cancel(id);
    if (mounted && version === revision) {
      await loadDetail();
      await loadList();
    }
  } catch (e) {
    if (mounted && version === revision) error.value = message(e);
  }
}
function reset() {
  revision++;
  entries.value = [];
  detail.value = null;
  source.value = "";
  cleanSource.value = "";
  confirmed.value = false;
  more.value = false;
  error.value = "";
  state.value = importState(project.value);
  selected.value =
    typeof route.query.operation === "string" ? route.query.operation : "";
  void loadList();
  void loadDetail();
}
watch(project, reset);
watch(
  () => route.query.operation,
  (id) => {
    if (typeof id === "string" && id !== selected.value) choose(id);
    else if (!id && selected.value) closeDetail();
  },
);
onMounted(() => {
  reset();
  timer = setInterval(() => {
    void loadList();
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
function closeDetail() {
  detailClosed.value = true;
  selected.value = "";
  detail.value = null;
  revision++;
  void router.replace({ query: { ...route.query, operation: undefined } });
}
</script>
<template>
  <section class="page import-page">
    <h2>旧版历史导入</h2>
    <p>
      选择服务所在主机上的 v0.2
      数据库路径，先预检，再确认导入固定的脱敏内容。原数据库不修改；旧 Provider
      配置不会恢复。导入后的会话只能从历史页查看，不能继续执行。
    </p>
    <p>
      上限：数据库及 WAL 合计
      64MiB、5万行、32MiB文本，读取最多10秒。旧总量作为历史资料保留，不加入现代逐次调用账本。
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="state.pending">
      上次请求尚未确认。<button
        :disabled="state.active"
        @click="submit(state.pending!)"
      >
        重试原导入请求
      </button>
    </p>
    <form @submit.prevent="preview">
      <label
        >旧数据库路径<input
          v-model="source"
          required
          maxlength="4096"
          :disabled="locked || busy"
      /></label>
      <button :disabled="locked || busy || !source.trim()">预检旧数据库</button>
    </form>
    <h3>导入操作历史</h3>
    <div class="import-list">
      <button
        v-for="entry in entries"
        :key="entry.run_id"
        @click="choose(entry.run_id)"
      >
        {{ entry.action === "preview" ? "预检" : "导入" }} ·
        {{ labels[entry.status] }} · {{ entry.run_id }}
      </button>
    </div>
    <button v-if="more" :disabled="loadingMore" @click="loadList(true)">
      加载更早操作
    </button>
    <article v-if="detail" aria-label="历史导入详情">
      <PanelClose @close="closeDetail" />
      <h3>
        {{ detail.action === "preview" ? "预检" : "导入" }} ·
        {{ labels[detail.status] }}
      </h3>
      <p>{{ detail.run_id }}</p>
      <p v-if="detail.error">{{ detail.error }}</p>
      <template v-if="detail.preview">
        <p>
          会话 {{ detail.preview.counts.sessions }} · 运行
          {{ detail.preview.counts.runs }} · 消息
          {{ detail.preview.counts.messages }} · 原始事件
          {{ detail.preview.counts.events }}
        </p>
        <p>内容 SHA256：{{ detail.preview.sha256 }}</p>
        <p v-if="detail.imported">这份预检内容已经导入，不能重复导入。</p>
        <template v-else-if="detail.status === 'completed'">
          <label
            ><input
              type="checkbox"
              v-model="confirmed"
              :disabled="locked || busy"
            />确认将这份预检内容导入当前项目的只读历史</label
          >
          <button :disabled="!confirmed || locked || busy" @click="apply">
            确认导入历史
          </button>
        </template></template
      >
      <template v-if="detail.result">
        <p>
          已导入 {{ detail.result.sessions }} 条只读会话、{{
            detail.result.runs
          }}
          条历史运行。
        </p>
        <RouterLink
          :to="{
            path: '/history',
            query: { session: detail.result.first_session_id || undefined },
          }"
          >查看导入历史</RouterLink
        >
      </template>
      <button
        v-if="['queued', 'running'].includes(detail.status)"
        @click="cancel"
      >
        停止导入操作
      </button>
      <p>
        已进入事务的导入会等待提交或回滚，停止不会撤销已提交的数据。重启后的未完成任务不会自动重放。
      </p>
    </article>
  </section>
</template>
<style scoped>
.import-page {
  max-width: 1000px;
  margin: auto;
  overflow-wrap: anywhere;
}
form,
.import-list {
  display: grid;
  gap: 0.7rem;
  margin: 1rem 0;
}
form label {
  display: grid;
  gap: 0.4rem;
}
input:not([type="checkbox"]) {
  width: 100%;
  box-sizing: border-box;
}
button {
  white-space: normal;
  overflow-wrap: anywhere;
}
article {
  border-top: 1px solid var(--border);
  padding-top: 1rem;
}
</style>
