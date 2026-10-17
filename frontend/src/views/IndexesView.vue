<script setup lang="ts">
import PanelClose from "../components/PanelClose.vue";
const detailClosed = ref(false);
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  indexesApi,
  indexRequestState,
  observeIndexes,
  type IndexConfiguration,
  type IndexDetail,
} from "../api/indexes";
import { useWorkbench } from "../stores/workbench";
const store = useWorkbench(),
  route = useRoute(),
  router = useRouter();
const project = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.project_id || "",
);
const config = ref<IndexConfiguration | null>(null),
  entries = ref<IndexDetail[]>([]),
  detail = ref<IndexDetail | null>(null);
const error = ref(""),
  listError = ref(""),
  detailError = ref(""),
  configError = ref("");
const force = ref(false),
  structured = ref(false),
  loading = ref(false),
  stopping = ref(false),
  more = ref(false),
  moreLoading = ref(false);
const state = ref(indexRequestState(project.value));
const selected = ref("");
const labels: Record<string, string> = {
  queued: "排队中",
  running: "构建中",
  completed: "构建完成",
  interrupted: "已中断",
  failed: "构建失败",
};
const busy = computed(() =>
  entries.value.some((e) => ["queued", "running"].includes(e.status)),
);
const locked = computed(() => state.value.active || !!state.value.pending);
const activeDetail = computed(
  () => !!detail.value && ["queued", "running"].includes(detail.value.status),
);
let mounted = true,
  revision = 0,
  detailRevision = 0,
  listFlight = -1,
  detailFlight = "",
  timer: ReturnType<typeof setInterval> | undefined;
const receipts = new Map<string, string>();
function sync() {
  state.value = indexRequestState(project.value);
  if (state.value.pending) {
    force.value = state.value.pending.force;
    structured.value = state.value.pending.code_structure;
  }
  const receipt = state.value.receipt;
  if (receipt && receipts.get(project.value) !== receipt.run_id) {
    receipts.set(project.value, receipt.run_id);
    if (!detailClosed.value) choose(receipt.run_id);
    void loadList();
  }
}
const stopObserving = observeIndexes(sync);
function queryId() {
  return typeof route.query.index === "string" ? route.query.index : "";
}
function choose(id: string) {
  detailClosed.value = !id;
  if (!mounted) return;
  if (selected.value !== id) {
    selected.value = id;
    detail.value = null;
    detailRevision++;
    stopping.value = false;
  }
  if (queryId() !== id)
    void router.replace({ query: { ...route.query, index: id || undefined } });
  void loadDetail();
}
async function loadConfig() {
  const current = revision;
  try {
    const value = await indexesApi.configuration();
    if (mounted && current === revision) {
      config.value = value;
      configError.value = "";
    }
  } catch (e) {
    if (mounted && current === revision)
      configError.value = e instanceof Error ? e.message : "配置读取失败";
  }
}
async function loadList() {
  const current = revision,
    owner = project.value;
  if (!owner || listFlight === current) return;
  listFlight = current;
  loading.value = true;
  try {
    const values = await indexesApi.list(owner);
    if (!mounted || current !== revision) return;
    const ids = new Set(values.map((e) => e.run_id));
    entries.value = [
      ...values,
      ...entries.value.filter((e) => !ids.has(e.run_id)),
    ];
    if (entries.value.length <= 20) more.value = values.length === 20;
    listError.value = "";
    if (!detailClosed.value && !selected.value && values[0])
      choose(values[0].run_id);
  } catch (e) {
    if (mounted && current === revision)
      listError.value = e instanceof Error ? e.message : "历史读取失败";
  } finally {
    if (current === revision) {
      listFlight = -1;
      loading.value = false;
    }
  }
}
async function loadMore() {
  if (moreLoading.value || !entries.value.length) return;
  const current = revision,
    owner = project.value;
  moreLoading.value = true;
  try {
    const values = await indexesApi.list(owner, entries.value.at(-1)!.run_id);
    if (!mounted || current !== revision) return;
    const ids = new Set(entries.value.map((e) => e.run_id));
    entries.value.push(...values.filter((e) => !ids.has(e.run_id)));
    more.value = values.length === 20;
  } catch (e) {
    if (mounted && current === revision)
      listError.value = e instanceof Error ? e.message : "历史读取失败";
  } finally {
    if (current === revision) moreLoading.value = false;
  }
}
async function loadDetail() {
  const current = revision,
    version = detailRevision,
    id = selected.value,
    owner = project.value;
  const flight = `${current}:${version}:${id}`;
  if (!id || detailFlight === flight) return;
  detailFlight = flight;
  try {
    const value = await indexesApi.detail(id);
    if (!mounted || current !== revision || version !== detailRevision) return;
    if (value.project_id !== owner) throw new Error("该索引记录属于其他项目。");
    detail.value = value;
    detailError.value = "";
  } catch (e) {
    if (mounted && current === revision && version === detailRevision)
      detailError.value = e instanceof Error ? e.message : "详情读取失败";
  } finally {
    if (detailFlight === flight) detailFlight = "";
  }
}
async function start() {
  const current = revision,
    owner = project.value;
  if (
    !owner ||
    state.value.active ||
    (!state.value.pending && (!config.value || busy.value))
  )
    return;
  const body = state.value.pending || {
    config_hash: config.value!.config_hash,
    force: force.value,
    code_structure: structured.value,
  };
  error.value = "";
  try {
    await indexesApi.create(owner, body);
  } catch (e) {
    if (mounted && current === revision) {
      error.value = e instanceof Error ? e.message : "索引接收失败";
      void loadConfig();
    }
  }
}
async function stop() {
  if (!detail.value || stopping.value) return;
  const current = revision,
    version = detailRevision,
    id = detail.value.run_id;
  stopping.value = true;
  try {
    const value = await indexesApi.cancel(id);
    if (mounted && current === revision && version === detailRevision)
      detail.value = value;
    void loadList();
  } catch (e) {
    if (mounted && current === revision && version === detailRevision)
      error.value = e instanceof Error ? e.message : "停止失败";
  } finally {
    if (current === revision && version === detailRevision)
      stopping.value = false;
  }
}
function reset() {
  revision++;
  detailRevision++;
  entries.value = [];
  detail.value = null;
  config.value = null;
  selected.value = queryId();
  error.value = "";
  listError.value = "";
  detailError.value = "";
  configError.value = "";
  more.value = false;
  moreLoading.value = false;
  stopping.value = false;
  force.value = false;
  structured.value = false;
  sync();
  void loadConfig();
  void loadList();
  void loadDetail();
}
watch(project, reset);
watch(
  () => route.query.index,
  () => {
    if (queryId() !== selected.value) choose(queryId());
  },
);
onMounted(() => {
  reset();
  timer = setInterval(() => {
    void loadList();
    if (activeDetail.value) void loadDetail();
  }, 2000);
});
onUnmounted(() => {
  mounted = false;
  clearInterval(timer);
  stopObserving();
});
function closeDetail() {
  detailClosed.value = true;
  selected.value = "";
  detail.value = null;
  detailRevision++;
  void router.replace({ query: { ...route.query, index: undefined } });
}
</script>
<template>
  <section class="page indexes">
    <h2>知识索引</h2>
    <p>
      为当前项目建立可检索的代码与文档索引。构建使用共享队列和预算；刷新或切页不会停止任务。
    </p>
    <p v-if="config">
      模型：{{ config.provider }} / {{ config.model }}。{{
        config.provider === "hash"
          ? "Hash 仅验证检索机制，不代表语义模型。"
          : config.provider === "openai_compatible"
            ? "远程调用按实际用量计入当前项目预算。"
            : "使用预先准备的本地模型进行计算。"
      }}
    </p>
    <p v-if="configError" class="inline-error">{{ configError }}</p>
    <div class="build-controls">
      <label
        ><input
          v-model="force"
          type="checkbox"
          :disabled="locked"
        />重新构建全部内容</label
      >
      <label
        ><input
          v-model="structured"
          type="checkbox"
          :disabled="locked"
        />代码结构切块</label
      >
      <button
        :disabled="
          !project || state.active || (!state.pending && (!config || busy))
        "
        @click="start"
      >
        {{
          state.active
            ? "正在接收…"
            : state.pending
              ? "重试原索引请求"
              : "建立索引"
        }}
      </button>
    </div>
    <p v-if="state.pending" class="muted">
      上次接收尚未确认，重试沿用原请求身份与配置。
    </p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p class="muted">
      增量构建复用未变化内容。配置变化需重建；取消或失败保留旧索引。索引不修改源代码。可选模型需在服务端预先准备，不自动下载。
    </p>
    <h3>构建历史</h3>
    <p v-if="listError" class="inline-error">{{ listError }}</p>
    <p v-if="!entries.length">{{ loading ? "读取中…" : "暂无构建记录。" }}</p>
    <div class="index-list">
      <button
        v-for="entry in entries"
        :key="entry.run_id"
        :class="{ selected: selected === entry.run_id }"
        @click="choose(entry.run_id)"
      >
        {{ labels[entry.status] || entry.status }} · {{ entry.run_id }}
      </button>
    </div>
    <button v-if="more" :disabled="moreLoading" @click="loadMore">
      加载更早记录
    </button>
    <p v-if="detailError" class="inline-error">{{ detailError }}</p>
    <article v-if="detail">
      <PanelClose @close="closeDetail" />
      <h3>{{ labels[detail.status] || detail.status }}</h3>
      <p class="identity">{{ detail.run_id }}</p>
      <button v-if="activeDetail" :disabled="stopping" @click="stop">
        {{ stopping ? "等待清理…" : "停止构建" }}
      </button>
      <p v-if="detail.error" class="inline-error">{{ detail.error }}</p>
      <p v-if="detail.published">
        发布代次 {{ detail.published.generation }} ·
        {{ detail.published.stats.files }} 个文件 /
        {{ detail.published.stats.chunks }} 个片段；重建
        {{ detail.published.stats.rebuilt_files }} 个文件，移除
        {{ detail.published.stats.deleted_files }} 个文件。
      </p>
      <p v-else class="muted">
        本次尚无已保存的发布证据。重启后的任务不会自动重放，请明确发起新构建。
      </p>
      <RouterLink :to="{ path: '/usage', query: { run: detail.run_id } }"
        >查看用量与预算</RouterLink
      >
    </article>
  </section>
</template>
<style scoped>
.indexes {
  max-width: 1000px;
  margin: auto;
  overflow-wrap: anywhere;
}
.build-controls {
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
  align-items: center;
}
.build-controls label {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.index-list {
  display: grid;
  gap: 0.5rem;
  margin-bottom: 1rem;
}
.index-list button {
  text-align: left;
  overflow-wrap: anywhere;
  white-space: normal;
}
.selected {
  border-color: var(--accent);
}
article {
  border-top: 1px solid var(--border);
  margin-top: 1rem;
  padding-top: 1rem;
}
.identity {
  font-size: 0.85rem;
}
</style>
