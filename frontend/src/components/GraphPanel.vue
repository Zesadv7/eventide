<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { graphsApi, type AgentGraph, type GraphNodeInput } from "../api/graphs";

const props = defineProps<{ runId: string }>();
const graphs = ref<AgentGraph[]>([]),
  title = ref(""),
  draftNodes = ref<GraphNodeInput[]>([
    { id: "task_1", kind: "readonly", goal: "", depends_on: [] },
  ]),
  loading = ref(false),
  saving = ref(false),
  pending = ref<Record<string, boolean>>({}),
  error = ref(""),
  notice = ref("");
let scope = 0,
  revision = 0,
  nextNode = 2,
  poller: ReturnType<typeof setInterval> | null = null;

const complete = computed(
  () =>
    title.value.trim() && draftNodes.value.every((node) => node.goal.trim()),
);
const pollingNeeded = computed(() =>
  graphs.value.some(
    (graph) =>
      graph.started && ["ready", "running", "cancelling"].includes(graph.state),
  ),
);
const labels: Record<string, string> = {
  ready: "等待执行",
  pending: "等待前置任务",
  running: "执行中",
  stopping: "停止中",
  cancelling: "停止中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已停止",
  blocked: "前置任务未完成",
};

function stopPolling() {
  if (poller !== null) clearInterval(poller);
  poller = null;
}

async function load() {
  const current = ++revision,
    owner = scope,
    runId = props.runId;
  if (!runId) return;
  loading.value = true;
  try {
    const value = await graphsApi.list(runId);
    if (current === revision && owner === scope) {
      graphs.value = value.graphs;
      error.value = "";
    }
  } catch (e) {
    if (current === revision && owner === scope)
      error.value = e instanceof Error ? e.message : "任务图读取失败";
  } finally {
    if (current === revision && owner === scope) loading.value = false;
  }
}

function addNode() {
  if (saving.value || draftNodes.value.length >= 16) return;
  draftNodes.value.push({
    id: `task_${nextNode++}`,
    kind: "readonly",
    goal: "",
    depends_on: [],
  });
}

function removeNode(id: string) {
  draftNodes.value = draftNodes.value
    .filter((node) => node.id !== id)
    .map((node) => ({
      ...node,
      depends_on: node.depends_on.filter((dep) => dep !== id),
    }));
}

function updateGraph(value: AgentGraph) {
  revision++;
  loading.value = false;
  const index = graphs.value.findIndex(
    (graph) => graph.graph_id === value.graph_id,
  );
  if (index < 0) graphs.value.push(value);
  else graphs.value.splice(index, 1, value);
}

async function create() {
  if (!complete.value || saving.value) return;
  const owner = scope,
    runId = props.runId;
  saving.value = true;
  revision++;
  loading.value = false;
  error.value = "";
  notice.value = "";
  try {
    const value = await graphsApi.create(runId, {
      title: title.value.trim(),
      nodes: draftNodes.value.map((node) => ({
        ...node,
        goal: node.goal.trim(),
        depends_on: [...node.depends_on],
      })),
    });
    if (owner !== scope) return;
    updateGraph(value);
    title.value = "";
    nextNode = 2;
    draftNodes.value = [
      { id: "task_1", kind: "readonly", goal: "", depends_on: [] },
    ];
    notice.value = "方案已保存；点击“开始调查”后才执行。";
  } catch (e) {
    if (owner === scope)
      error.value = e instanceof Error ? e.message : "任务图保存失败";
  } finally {
    if (owner === scope) saving.value = false;
  }
}

async function act(graph: AgentGraph, action: "advance" | "cancel") {
  if (pending.value[graph.graph_id]) return;
  const owner = scope;
  pending.value = { ...pending.value, [graph.graph_id]: true };
  revision++;
  loading.value = false;
  error.value = "";
  notice.value = "";
  try {
    const value = await graphsApi[action](graph.graph_id);
    if (owner === scope) updateGraph(value);
  } catch (e) {
    if (owner === scope)
      error.value = e instanceof Error ? e.message : "任务图操作失败";
  } finally {
    if (owner === scope) {
      const next = { ...pending.value };
      delete next[graph.graph_id];
      pending.value = next;
    }
  }
}

watch(
  () => props.runId,
  () => {
    scope++;
    revision++;
    stopPolling();
    graphs.value = [];
    title.value = "";
    draftNodes.value = [
      { id: "task_1", kind: "readonly", goal: "", depends_on: [] },
    ];
    nextNode = 2;
    error.value = "";
    notice.value = "";
    pending.value = {};
    saving.value = false;
    loading.value = false;
    void load();
  },
  { immediate: true },
);
watch(pollingNeeded, (needed) => {
  stopPolling();
  if (needed)
    poller = setInterval(() => {
      if (!loading.value && !saving.value && !Object.keys(pending.value).length)
        void load();
    }, 2000);
});
onUnmounted(() => {
  scope++;
  revision++;
  stopPolling();
});
</script>

<template>
  <details v-if="runId" class="graph-panel">
    <summary>
      协作任务图
      <span v-if="graphs.length" class="badge">{{ graphs.length }} 个</span>
    </summary>
    <p>
      把调查拆成多个只读任务，勾选前后依赖；开始后，前置任务完成会自动启动后续调查。各任务读取快照并共用主任务预算。
    </p>
    <p class="muted">
      本阶段仅支持只读调查；修改代码、候选整合和独立验收仍需使用已有交付流程。
    </p>
    <form @submit.prevent="create">
      <fieldset :disabled="saving">
        <legend>新建调查方案</legend>
        <label
          >方案名称
          <input
            v-model="title"
            maxlength="200"
            aria-label="任务图名称"
            placeholder="例如：调查登录失败"
        /></label>
        <div v-for="node in draftNodes" :key="node.id" class="graph-draft-node">
          <label
            >{{ node.id }} 的任务
            <textarea
              v-model="node.goal"
              :aria-label="`${node.id} 的任务`"
              rows="2"
              maxlength="2000"
              placeholder="说明需要调查什么"
            />
          </label>
          <fieldset v-if="draftNodes.length > 1" class="graph-dependencies">
            <legend>{{ node.id }} 等待哪些任务完成？</legend>
            <label
              v-for="other in draftNodes.filter(
                (candidate) => candidate.id !== node.id,
              )"
              :key="other.id"
            >
              <input
                v-model="node.depends_on"
                type="checkbox"
                :value="other.id"
                :aria-label="`${node.id} 依赖 ${other.id}`"
              />
              {{ other.id }}
            </label>
          </fieldset>
          <button
            v-if="draftNodes.length > 1"
            type="button"
            @click="removeNode(node.id)"
          >
            删除 {{ node.id }}
          </button>
        </div>
        <div class="button-row">
          <button
            type="button"
            :disabled="draftNodes.length >= 16"
            @click="addNode"
          >
            添加任务
          </button>
          <button class="primary" type="submit" :disabled="!complete">
            {{ saving ? "保存中…" : "保存方案" }}
          </button>
        </div>
      </fieldset>
    </form>
    <div class="button-row">
      <button
        :disabled="loading || saving || Object.keys(pending).length > 0"
        @click="load"
      >
        刷新任务图
      </button>
    </div>
    <p v-if="loading && !graphs.length" role="status">正在读取任务图…</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!graphs.length && !loading" class="muted">
      本次运行暂无已保存的任务图。
    </p>
    <article v-for="graph in graphs" :key="graph.graph_id" class="saved-graph">
      <h4>{{ graph.title }}</h4>
      <p>
        <span class="badge">{{
          !graph.started && !graph.cancelled
            ? "方案待开始"
            : labels[graph.state] || graph.state
        }}</span>
        {{ graph.counts.completed }} / {{ graph.counts.total }} 个任务已完成
      </p>
      <p v-if="graph.state === 'cancelling'" role="status">
        正在等待已启动的调查停止并清理快照…
      </p>
      <div class="button-row">
        <button
          v-if="!graph.started && !graph.cancelled"
          class="primary"
          :disabled="pending[graph.graph_id]"
          @click="act(graph, 'advance')"
        >
          {{ pending[graph.graph_id] ? "提交中…" : "开始调查" }}
        </button>
        <button
          v-if="
            !graph.cancelled && !['completed', 'failed'].includes(graph.state)
          "
          :disabled="pending[graph.graph_id]"
          @click="act(graph, 'cancel')"
        >
          {{
            pending[graph.graph_id]
              ? "提交中…"
              : graph.started
                ? "停止任务图"
                : "取消方案"
          }}
        </button>
      </div>
      <ol class="graph-nodes">
        <li v-for="node in graph.nodes" :key="node.id">
          <p>
            <strong>{{ node.id }}</strong> ·
            {{ labels[node.state] || node.state }}
          </p>
          <p>{{ node.goal }}</p>
          <p class="muted">
            {{
              node.depends_on.length
                ? `${node.depends_on.join("、")} → ${node.id}`
                : "起点任务 · 无前置依赖"
            }}
          </p>
          <p v-if="node.claim" class="muted mono">
            运行记录：{{ node.claim.run_id }}
          </p>
          <RouterLink
            v-if="node.result?.child_session_id || node.claim?.child_session_id"
            :to="{
              path: '/history',
              query: {
                session:
                  node.result?.child_session_id || node.claim?.child_session_id,
                run: node.result?.run_id || node.claim?.run_id,
              },
            }"
            >查看此任务的运行记录</RouterLink
          >
          <p v-if="node.result?.error" role="alert">{{ node.result.error }}</p>
          <p v-if="node.result?.output_truncated" class="muted">
            此处为结果节选<span v-if="node.result.output_length"
              >（原结果 {{ node.result.output_length }} 字符）</span
            >；完整内容请查看此任务的运行记录。
          </p>
          <details v-if="node.result?.output">
            <summary>调查结果</summary>
            <pre>{{ node.result.output }}</pre>
          </details>
        </li>
      </ol>
    </article>
  </details>
</template>

<style scoped>
.graph-panel {
  min-width: 0;
}
.graph-panel fieldset {
  min-width: 0;
  margin: 0.75rem 0;
}
.graph-panel input:not([type="checkbox"]),
.graph-panel textarea {
  box-sizing: border-box;
  width: 100%;
}
.graph-draft-node {
  margin-block: 0.75rem;
}
.graph-dependencies {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.saved-graph {
  border-top: 1px solid var(--line);
  margin-top: 1rem;
  padding-top: 0.5rem;
}
.graph-nodes {
  padding-left: 1.4rem;
}
.graph-nodes p,
.graph-nodes pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.graph-nodes pre {
  max-height: 24rem;
  overflow: auto;
}
</style>
