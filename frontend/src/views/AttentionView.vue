<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { attentionApi, type AttentionSnapshot } from "../api/attention";
import { api, ApiError } from "../api/client";
import { useWorkbench } from "../stores/workbench";

const store = useWorkbench(),
  router = useRouter(),
  route = useRoute();
const project = computed(() =>
  typeof route.query.project === "string" ? route.query.project : "",
);
const projects = computed(() => {
  const result = new Map<string, string>();
  for (const workspace of store.workspaces) {
    if (workspace.project_id && !result.has(workspace.project_id))
      result.set(workspace.project_id, workspace.name);
  }
  return [...result].map(([id, name]) => ({ id, name }));
});
function selectProject(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  void router.replace({
    query: { ...route.query, project: value || undefined },
  });
}
const state = ref<AttentionSnapshot | null>(null),
  error = ref(""),
  loading = ref(false);
const deciding = ref<Record<string, boolean>>({});
const expanded = ref<Record<string, boolean>>({});
const actionError = ref<Record<string, string>>({});
let revision = 0;
let mounted = true,
  timer: ReturnType<typeof setInterval> | undefined;

async function load() {
  if (loading.value) return;
  loading.value = true;
  const version = revision;
  const selected = project.value;
  try {
    const value = await (selected
      ? attentionApi.project(selected)
      : attentionApi.global());
    if (mounted && version === revision) {
      state.value = value;
      error.value = "";
    }
  } catch (e) {
    if (mounted && version === revision)
      error.value = e instanceof Error ? e.message : "待处理读取失败";
  } finally {
    if (mounted && version === revision) loading.value = false;
  }
}
watch(
  project,
  () => {
    revision += 1;
    state.value = null;
    error.value = "";
    expanded.value = {};
    actionError.value = {};
    loading.value = false;
    void load();
  },
  { immediate: true },
);
onMounted(() => {
  timer = setInterval(() => void load(), 5000);
});
onUnmounted(() => {
  mounted = false;
  clearInterval(timer);
});

function projectName(workspaceId: string) {
  return (
    store.workspaces.find((item) => item.workspace_id === workspaceId)?.name ||
    "项目未记录"
  );
}
function toggle(key: string) {
  expanded.value[key] = !expanded.value[key];
}
async function decide(approvalId: string, runId: string, approved: boolean) {
  const key = `${runId}:${approvalId}`;
  if (deciding.value[key]) return;
  deciding.value[key] = true;
  actionError.value[key] = "";
  const version = revision;
  try {
    await api.approve(runId, approvalId, approved);
    if (mounted && version === revision) await load();
  } catch (e) {
    if (mounted && version === revision)
      actionError.value[key] =
        e instanceof ApiError && e.status === 409
          ? "审批已失效或运行已结束，请刷新列表"
          : e instanceof Error
            ? e.message
            : "审批请求失败";
  } finally {
    delete deciding.value[key];
  }
}
async function openSession(sessionId: string, workspaceId: string) {
  try {
    if (store.selectedWorkspace !== workspaceId)
      await store.selectWorkspace(workspaceId);
    await router.push(`/chat/${sessionId}`);
  } catch {
    actionError.value[sessionId] = "会话打开失败";
  }
}
function undeclared(item: { network_access: null; privilege_elevation: null }) {
  const missing: string[] = [];
  if (item.network_access === null) missing.push("网络访问未声明");
  if (item.privilege_elevation === null) missing.push("权限提升未声明");
  return missing;
}
function formatTool(summary: {
  tool: string | null;
  mcp_server: string | null;
  mcp_tool: string | null;
}) {
  if (summary.mcp_server) return `${summary.mcp_server} / ${summary.mcp_tool}`;
  return summary.tool || "未知工具";
}
</script>

<template>
  <section class="page attention-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">ATTENTION / INBOX</p>
        <h1>待处理中心</h1>
        <p class="muted">
          汇总待审批、失败运行与待采纳候选，来源于任务的实际记录。你可以处理审批、查看失败原因，或检查待采纳的修改。
        </p>
      </div>
      <button :disabled="loading" @click="load">刷新</button>
    </header>
    <label class="project-filter">
      查看范围
      <select
        aria-label="待处理项目范围"
        :value="project"
        @change="selectProject"
      >
        <option value="">所有项目</option>
        <option v-for="item in projects" :key="item.id" :value="item.id">
          {{ item.name }}
        </option>
        <option
          v-if="project && !projects.some((item) => item.id === project)"
          :value="project"
        >
          项目 {{ project }}（当前目录未列出）
        </option>
      </select>
    </label>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <template v-if="state">
      <p v-if="state.limits" class="muted">
        当前最多展示 {{ state.limits.approvals }} 条最近审批和
        {{ state.limits.runs }} 条最近失败或停驻记录；下列数字是本页显示数量。
      </p>
      <p class="muted">
        待审批 {{ state.counts.approvals }} · 失败/停驻运行
        {{ state.counts.runs }} · 待采纳候选
        {{ state.counts.delivery_candidates }}
      </p>

      <section class="panel">
        <h2>待审批</h2>
        <p v-if="!state.approvals.length" class="muted">当前没有待审批请求。</p>
        <article
          v-for="item in state.approvals"
          :key="item.dedup_key"
          class="attention-item"
        >
          <header class="attention-head">
            <strong>{{ formatTool(item.summary) }}</strong>
            <span class="badge" :class="{ stale: !item.actionable }">{{
              item.actionable ? "等待决定" : "已失效（运行已结束）"
            }}</span>
            <small>{{ projectName(item.workspace_id) }}</small>
            <small v-if="item.ts">{{
              new Date(item.ts * 1000).toLocaleString("zh-CN")
            }}</small>
          </header>
          <p v-if="item.reason" class="muted">原因：{{ item.reason }}</p>
          <dl class="attention-facts">
            <template v-if="item.summary.command">
              <dt>命令</dt>
              <dd>
                <code>{{ item.summary.command }}</code>
              </dd>
            </template>
            <template v-if="item.summary.targets.length">
              <dt>目标路径</dt>
              <dd>
                <code v-for="target in item.summary.targets" :key="target">{{
                  target
                }}</code>
              </dd>
            </template>
            <template v-if="item.summary.query">
              <dt>查询</dt>
              <dd>{{ item.summary.query }}</dd>
            </template>
            <dt>执行位置</dt>
            <dd>
              {{ item.summary.execution_label || "未声明" }}
              <small v-if="item.summary.cwd" class="mono">{{
                item.summary.cwd
              }}</small>
            </dd>
            <dt>影响范围</dt>
            <dd>{{ item.summary.impact || "未声明" }}</dd>
            <dt>未声明</dt>
            <dd>{{ undeclared(item.summary).join("；") || "无" }}</dd>
          </dl>
          <p class="muted">
            原始审批身份与参数未做展示转换；批准或拒绝都会把
            <code>{{ item.approval_id }}</code> 提交给原运行核验。
          </p>
          <button class="linklike" @click="toggle(item.dedup_key)">
            {{ expanded[item.dedup_key] ? "收起原始 JSON" : "展开原始 JSON" }}
          </button>
          <pre v-if="expanded[item.dedup_key]" class="mono json-block">{{
            JSON.stringify(item.arguments, null, 2)
          }}</pre>
          <div class="button-row">
            <button
              :disabled="
                !item.actionable ||
                deciding[`${item.run_id}:${item.approval_id}`]
              "
              @click="decide(item.approval_id, item.run_id, true)"
            >
              批准
            </button>
            <button
              :disabled="
                !item.actionable ||
                deciding[`${item.run_id}:${item.approval_id}`]
              "
              @click="decide(item.approval_id, item.run_id, false)"
            >
              拒绝
            </button>
            <button @click="openSession(item.session_id, item.workspace_id)">
              打开对话
            </button>
            <button @click="router.push(item.links.run)">查看运行</button>
          </div>
          <p
            v-if="actionError[`${item.run_id}:${item.approval_id}`]"
            class="inline-error"
            role="alert"
          >
            {{ actionError[`${item.run_id}:${item.approval_id}`] }}
          </p>
        </article>
      </section>

      <section class="panel">
        <h2>失败 / 停驻运行</h2>
        <p v-if="!state.runs.length" class="muted">
          当前没有失败或停驻的运行。
        </p>
        <article
          v-for="item in state.runs"
          :key="item.dedup_key"
          class="attention-item"
        >
          <header class="attention-head">
            <strong>{{
              item.status === "failed" ? "运行失败" : "运行停驻"
            }}</strong>
            <small>{{ projectName(item.workspace_id) }}</small>
            <small v-if="item.ts">{{
              new Date(item.ts * 1000).toLocaleString("zh-CN")
            }}</small>
          </header>
          <p v-if="item.error" class="muted">{{ item.error }}</p>
          <p v-else-if="item.reason" class="muted">{{ item.reason }}</p>
          <p v-if="item.pending_approvals" class="muted">
            该运行还有 {{ item.pending_approvals }} 个未关闭的审批事实。
          </p>
          <div class="button-row">
            <button @click="openSession(item.session_id, item.workspace_id)">
              打开对话
            </button>
            <button @click="router.push(item.links.run)">查看运行</button>
          </div>
        </article>
      </section>

      <section class="panel">
        <h2>待采纳交付候选</h2>
        <p v-if="!state.delivery_candidates.length" class="muted">
          当前没有通过验收、等待人工采纳的候选。
        </p>
        <article
          v-for="item in state.delivery_candidates"
          :key="item.dedup_key"
          class="attention-item"
        >
          <header class="attention-head">
            <strong>交付候选：{{ item.name || item.delivery_id }}</strong>
            <small class="mono" v-if="item.patch_sha256">{{
              item.patch_sha256.slice(0, 12)
            }}</small>
          </header>
          <p class="muted">
            候选不会自动应用；采纳需要在交付页面复核补丁后显式确认。
          </p>
          <div class="button-row">
            <button @click="router.push('/deliveries')">打开交付页</button>
          </div>
        </article>
      </section>

      <p v-for="(note, kind) in state.unsupported" :key="kind" class="muted">
        {{ note }}
      </p>
    </template>
    <p v-else-if="loading" class="empty-state">正在读取待处理事项…</p>
  </section>
</template>

<style scoped>
.project-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 16px;
}
.project-filter select {
  max-width: 100%;
}
.attention-page {
  min-width: 0;
  overflow-wrap: anywhere;
}
.attention-item {
  border: 1px solid var(--border, #ddd);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 12px;
}
.attention-head {
  display: flex;
  gap: 12px;
  align-items: baseline;
  flex-wrap: wrap;
}
.badge.stale {
  opacity: 0.6;
}
.attention-facts {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 16px;
  margin: 8px 0;
}
.attention-facts dt {
  color: var(--muted, #666);
}
.attention-facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.json-block {
  max-height: 320px;
  overflow: auto;
  padding: 8px;
  border-radius: 6px;
  background: var(--surface-2, #f5f5f5);
}
.linklike {
  background: none;
  border: none;
  color: inherit;
  text-decoration: underline;
  cursor: pointer;
  padding: 0;
}
</style>
