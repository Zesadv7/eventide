<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  historyApi,
  type HistoryRun,
  type HistorySession,
} from "../api/history";
import RunTrace from "../components/RunTrace.vue";
import { useWorkbench } from "../stores/workbench";

const store = useWorkbench(),
  route = useRoute(),
  router = useRouter();
const project = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.project_id || "",
);
const projectName = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.name || "请先选择项目",
);
const sessions = ref<HistorySession[]>([]),
  sessionId = ref(""),
  runs = ref<HistoryRun[]>([]),
  runId = ref(""),
  detail = ref<HistoryRun | null>(null);
const sessionLoading = ref(false),
  runsLoading = ref(false),
  detailLoading = ref(false),
  hasOlder = ref(false);
const sessionError = ref(""),
  runsError = ref(""),
  detailError = ref("");
const selectedSession = computed(() =>
  sessions.value.find((s) => s.session_id === sessionId.value),
);
const displayedRuns = computed(() => [...runs.value].reverse());
const evaluationId = computed(() =>
  detail.value?.execution?.kind === "evaluation"
    ? detail.value.execution.evaluation_id
    : "",
);
const labels: Record<string, string> = {
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  queued: "排队中",
  running: "运行中",
  waiting_for_user: "等待审批",
  prepared: "已准备",
  abandoned: "已放弃",
};
let mounted = true,
  projectRevision = 0,
  sessionRevision = 0,
  runRevision = 0;
let sessionFlight = -1,
  runsFlight = "",
  detailFlight = "",
  timer: ReturnType<typeof setInterval> | undefined;
function query(name: string) {
  const value = route.query[name];
  return typeof value === "string" ? value : "";
}
function selection(session: string, run = "") {
  void router.replace({
    query: { session: session || undefined, run: run || undefined },
  });
}
function resetRuns() {
  sessionRevision++;
  runRevision++;
  runs.value = [];
  runId.value = "";
  detail.value = null;
  hasOlder.value = false;
  runsLoading.value = false;
  detailLoading.value = false;
  runsError.value = "";
  detailError.value = "";
}
function applyQuery() {
  if (!project.value || sessionLoading.value) return;
  const wanted = query("session");
  if (wanted && !sessions.value.some((s) => s.session_id === wanted)) {
    sessionId.value = "";
    resetRuns();
    sessionError.value = "此会话不在当前项目中，请选择本项目的会话。";
    return;
  }
  const owner =
    wanted ||
    (sessions.value.some((s) => s.session_id === store.selectedSession)
      ? store.selectedSession
      : sessions.value.filter((s) => !s.archived).at(-1)?.session_id ||
        sessions.value.at(-1)?.session_id ||
        "");
  if (!owner) return;
  sessionError.value = "";
  if (!wanted) {
    selection(owner);
    return;
  }
  if (owner !== sessionId.value) {
    sessionId.value = owner;
    resetRuns();
    void loadRuns();
  }
  const selected = query("run");
  if (selected !== runId.value) {
    runId.value = selected;
    runRevision++;
    detail.value = null;
    detailLoading.value = false;
    detailError.value = "";
    if (selected) void loadDetail();
  }
  if (!selected && runs.value.length)
    selection(owner, runs.value.at(-1)!.run_id);
}
async function loadSessions() {
  const current = projectRevision,
    owner = project.value;
  if (!owner || sessionFlight === current) return;
  sessionFlight = current;
  sessionLoading.value = true;
  try {
    const values = await historyApi.sessions(owner);
    if (!mounted || current !== projectRevision) return;
    if (values.some((value) => value.project_id !== owner))
      throw new Error("服务返回了不属于当前项目的会话，已停止展示。");
    sessions.value = values;
    sessionError.value = "";
  } catch (e) {
    if (mounted && current === projectRevision)
      sessionError.value = e instanceof Error ? e.message : "会话历史读取失败";
  } finally {
    if (mounted && current === projectRevision) {
      sessionLoading.value = false;
      sessionFlight = -1;
      if (!sessionError.value) applyQuery();
    }
  }
}
async function loadRuns(older = false) {
  const current = projectRevision,
    revision = sessionRevision,
    owner = sessionId.value;
  const flight = `${current}:${revision}:${owner}`;
  if (!owner || runsFlight === flight) return;
  const before = older ? runs.value[0]?.run_id : undefined;
  if (older && !before) return;
  runsFlight = flight;
  runsLoading.value = true;
  try {
    const values = await historyApi.runs(owner, before);
    if (!mounted || current !== projectRevision || revision !== sessionRevision)
      return;
    if (values.some((value) => value.session_id !== owner))
      throw new Error("运行列表与所选会话不一致，已停止展示。");
    const ids = new Set(runs.value.map((value) => value.run_id));
    runs.value = older
      ? [...values.filter((value) => !ids.has(value.run_id)), ...runs.value]
      : values;
    hasOlder.value = values.length === 100;
    runsError.value = "";
    if (!runId.value && values.length) selection(owner, values.at(-1)!.run_id);
  } catch (e) {
    if (mounted && current === projectRevision && revision === sessionRevision)
      runsError.value = e instanceof Error ? e.message : "运行历史读取失败";
  } finally {
    if (runsFlight === flight) runsFlight = "";
    if (mounted && current === projectRevision && revision === sessionRevision)
      runsLoading.value = false;
  }
}
async function loadDetail() {
  const current = projectRevision,
    revision = runRevision,
    owner = sessionId.value,
    id = runId.value;
  const flight = `${current}:${revision}:${id}`;
  if (!owner || !id || detailFlight === flight) return;
  detailFlight = flight;
  detailLoading.value = true;
  try {
    const value = await historyApi.run(id);
    if (!mounted || current !== projectRevision || revision !== runRevision)
      return;
    if (value.run_id !== id || value.session_id !== owner)
      throw new Error("此运行不属于所选会话，已停止展示与导出。");
    detail.value = value;
    runs.value = runs.value.map((run) =>
      run.run_id === value.run_id ? { ...run, ...value } : run,
    );
    detailError.value = "";
  } catch (e) {
    if (mounted && current === projectRevision && revision === runRevision) {
      detail.value = null;
      detailError.value = e instanceof Error ? e.message : "运行详情读取失败";
    }
  } finally {
    if (detailFlight === flight) detailFlight = "";
    if (mounted && current === projectRevision && revision === runRevision)
      detailLoading.value = false;
  }
}
async function refresh() {
  const current = projectRevision,
    revision = sessionRevision;
  await loadSessions();
  if (mounted && current === projectRevision && revision === sessionRevision) {
    void loadRuns();
    void loadDetail();
  }
}
watch(
  project,
  (value, previous) => {
    projectRevision++;
    sessions.value = [];
    sessionId.value = "";
    sessionLoading.value = false;
    sessionError.value = "";
    resetRuns();
    if (previous) selection("");
    if (value) void loadSessions();
  },
  { immediate: true },
);
watch(() => [route.query.session, route.query.run], applyQuery);
onMounted(() => {
  timer = setInterval(() => {
    if (
      detail.value &&
      ["queued", "running", "waiting_for_user"].includes(detail.value.status)
    )
      void loadDetail();
  }, 3000);
});
onUnmounted(() => {
  mounted = false;
  projectRevision++;
  sessionRevision++;
  runRevision++;
  clearInterval(timer);
});
function time(value?: number) {
  return value === undefined
    ? "未记录"
    : new Date(value * 1000).toLocaleString("zh-CN");
}
function toolLabel(value: unknown) {
  if (!value || typeof value !== "object") return "工具证据";
  const tool = value as Record<string, unknown>;
  return `${typeof tool.name === "string" ? tool.name : "工具"} · ${typeof tool.status === "string" ? labels[tool.status] || tool.status : "状态未记录"}`;
}
</script>

<template>
  <section class="page history-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">HISTORY / RUN EVIDENCE</p>
        <h1>历史运行</h1>
        <p class="muted">
          {{ projectName }} · 浏览历史不会切换当前对话或停止后台任务。
        </p>
      </div>
      <button :disabled="sessionLoading || runsLoading" @click="refresh">
        刷新历史
      </button>
    </header>
    <p
      v-for="message in [sessionError, runsError, detailError].filter(Boolean)"
      :key="message"
      class="inline-error"
      role="alert"
    >
      {{ message }}
    </p>
    <section class="panel" aria-label="选择历史会话">
      <label for="history-session">项目会话（含归档）</label
      ><select
        id="history-session"
        :value="sessionId"
        :disabled="sessionLoading || !sessions.length"
        @change="selection(($event.target as HTMLSelectElement).value)"
      >
        <option value="" disabled>选择会话</option>
        <option
          v-for="session in sessions"
          :key="session.session_id"
          :value="session.session_id"
        >
          {{ session.title }}{{ session.archived ? " · 已归档" : "" }} ·
          {{ session.session_id }}
        </option>
      </select>
      <p v-if="selectedSession" class="muted">
        {{ selectedSession.working_directory
        }}<br />这里只查看已有记录；归档会话及后台控制任务同样保留历史。
      </p>
      <p v-else class="muted">
        {{ sessionLoading ? "正在读取会话…" : "选择当前项目的会话查看记录。" }}
      </p>
    </section>
    <section v-if="sessionId" class="panel" aria-label="历史运行列表">
      <h2>运行记录</h2>
      <p class="muted">
        已加载
        {{ runs.length }}
        条，最新在前；这不是会话总数。刷新列表会回到最新100条，已选详情保持独立。
      </p>
      <div class="run-history-list">
        <button
          v-for="run in displayedRuns"
          :key="run.run_id"
          :class="{ selected: runId === run.run_id }"
          @click="selection(sessionId, run.run_id)"
        >
          <span
            >{{ labels[run.status] || run.status }} ·
            {{ time(run.started_at) }}</span
          ><small class="mono">{{ run.run_id }}</small
          ><span class="run-preview">{{ run.output || "暂无已保存输出" }}</span>
        </button>
      </div>
      <p v-if="!runs.length" class="muted">
        {{ runsLoading ? "正在读取运行记录…" : "此会话尚无运行。" }}
      </p>
      <button v-if="hasOlder" :disabled="runsLoading" @click="loadRuns(true)">
        {{ runsLoading ? "正在读取…" : "加载更早运行" }}
      </button>
    </section>
    <section
      v-if="detail"
      class="panel history-detail"
      aria-label="历史运行详情"
    >
      <header class="page-heading">
        <div>
          <h2>{{ labels[detail.status] || detail.status }}</h2>
          <p class="mono">{{ detail.run_id }}</p>
        </div>
        <a :href="historyApi.exportUrl(detail.run_id)" download
          >下载事件 JSONL</a
        >
      </header>
      <p class="muted">
        开始：{{ time(detail.started_at) }} · 结束：{{
          time(detail.completed_at)
        }}<br />步骤：{{ detail.steps ?? "未记录" }} · 工具调用：{{
          detail.tool_calls ?? "未记录"
        }}
      </p>
      <p v-if="detail.execution?.kind === 'worktree_control'">
        <RouterLink
          :to="{ path: '/worktrees', query: { operation: detail.run_id } }"
          >查看副本操作</RouterLink
        >
      </p>
      <p v-if="detail.execution?.kind === 'delivery'" class="evaluation-note">
        这是交付控制记录。<RouterLink
          :to="{
            path: '/deliveries',
            query: { delivery: detail.execution.delivery_id },
          }"
          >查看交付与验收</RouterLink
        >
      </p>
      <p v-if="detail.execution?.kind === 'index'" class="evaluation-note">
        这是索引构建记录。<RouterLink
          :to="{ path: '/knowledge', query: { index: detail.run_id } }"
          >查看索引构建</RouterLink
        >
      </p>
      <p v-if="evaluationId" class="evaluation-note">
        这是离线评测控制记录。<RouterLink
          :to="{ path: '/evaluations', query: { evaluation: evaluationId } }"
          >查看评测进度与报告</RouterLink
        >
      </p>
      <p v-if="detail.error" class="inline-error">{{ detail.error }}</p>
      <p v-if="detail.reason" class="muted">停止原因：{{ detail.reason }}</p>
      <h3>已保存输出</h3>
      <pre class="run-output">{{ detail.output || "暂无已保存输出。" }}</pre>
      <p class="muted">
        运行完成不等于变更已验收。运行中的记录每3秒更新；本页不发起审批、继续或修改任务。
      </p>
      <RunTrace :run-id="detail.run_id" />
      <h3>工具证据</h3>
      <details
        v-for="(tool, id) in detail.tools || {}"
        :key="id"
        class="tool-evidence"
      >
        <summary>
          {{ toolLabel(tool) }}<small class="mono">{{ id }}</small>
        </summary>
        <pre>{{ JSON.stringify(tool, null, 2) }}</pre>
      </details>
      <p v-if="!Object.keys(detail.tools || {}).length" class="muted">
        此运行未返回工具证据，可下载事件记录核对。
      </p>
      <details>
        <summary>运行身份与执行位置</summary>
        <pre>{{
          JSON.stringify(
            {
              session_id: detail.session_id,
              execution: detail.execution,
              continuation_of: detail.continuation_of,
              parent_run_id: detail.parent_run_id,
              gap_files: detail.gap_files,
            },
            null,
            2,
          )
        }}</pre>
      </details>
    </section>
    <p v-else-if="detailLoading" class="muted">正在读取所选运行详情…</p>
  </section>
</template>

<style scoped>
.history-page {
  overflow-wrap: anywhere;
}
.history-page .page-heading > button {
  flex-shrink: 0;
}
.history-page select {
  width: 100%;
  margin-top: 10px;
}
.run-history-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.run-history-list button {
  display: grid;
  gap: 8px;
  text-align: left;
  min-width: 0;
}
.run-history-list button.selected {
  background: var(--soft);
  border-color: var(--accent);
}
.run-history-list small {
  color: var(--muted);
}
.run-preview {
  font-size: 13px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.tool-evidence small {
  display: block;
  margin-top: 5px;
}
.tool-evidence pre,
.run-output {
  max-height: 500px;
  overflow: auto;
}
.history-detail .page-heading {
  align-items: flex-start;
  flex-wrap: wrap;
}
.evaluation-note {
  padding: 12px;
  background: var(--soft);
  border-radius: 8px;
}
</style>
