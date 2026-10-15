<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api/client";
import { queueApi, type QueueEntry, type QueueSnapshot } from "../api/queue";
import { useWorkbench } from "../stores/workbench";

const store = useWorkbench(),
  router = useRouter();
const state = ref<QueueSnapshot | null>(null),
  error = ref(""),
  loading = ref(false);
const stopping = ref<Record<string, boolean>>({});
let mounted = true,
  timer: ReturnType<typeof setInterval> | undefined;
const labels = {
  queued: "排队中",
  starting: "准备执行",
  running: "执行中",
  finishing: "收尾中",
};
async function load() {
  if (loading.value) return;
  loading.value = true;
  try {
    const value = await queueApi.status();
    if (mounted) {
      state.value = value;
      error.value = "";
    }
  } catch (e) {
    if (mounted) error.value = e instanceof Error ? e.message : "队列读取失败";
  } finally {
    if (mounted) loading.value = false;
  }
}
onMounted(() => {
  void load();
  timer = setInterval(() => void load(), 3000);
});
onUnmounted(() => {
  mounted = false;
  clearInterval(timer);
});
async function stop(entry: QueueEntry) {
  if (stopping.value[entry.run_id]) return;
  stopping.value[entry.run_id] = true;
  try {
    await api.cancel(entry.run_id);
    await load();
  } catch (e) {
    if (mounted) error.value = e instanceof Error ? e.message : "停止请求失败";
  } finally {
    delete stopping.value[entry.run_id];
  }
}
async function open(entry: QueueEntry) {
  try {
    if (store.selectedWorkspace !== entry.workspace_id)
      await store.selectWorkspace(entry.workspace_id);
    if (entry.execution.kind === "evaluation") {
      await router.push({
        path: "/evaluations",
        query: { evaluation: entry.execution.evaluation_id || entry.run_id },
      });
      return;
    }
    await store.selectSession(entry.session_id);
    await router.push(`/chat/${encodeURIComponent(entry.session_id)}`);
  } catch (e) {
    error.value = e instanceof Error ? e.message : "会话打开失败";
  }
}
function project(entry: QueueEntry) {
  return (
    store.workspaces.find((item) => item.workspace_id === entry.workspace_id)
      ?.name ||
    entry.project_id ||
    "项目未记录"
  );
}
function target(entry: QueueEntry) {
  if (entry.execution.kind === "evaluation") return "离线评测临时项目";
  return entry.execution.kind === "snapshot"
    ? "固定只读快照"
    : entry.execution.kind === "worktree"
      ? "独立工作副本"
      : "源工作副本";
}
</script>

<template>
  <section class="page queue-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">QUEUE / EXECUTION</p>
        <h1>任务队列</h1>
        <p class="muted">
          额度由整个服务共用。刷新或关闭页面不会停止已接收任务。
        </p>
      </div>
      <button :disabled="loading" @click="load">刷新</button>
    </header>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <template v-if="state">
      <div class="stat-grid">
        <article>
          <small>执行任务</small
          ><strong
            >{{ state.counts.active_roots }} /
            {{ state.limits.active_roots }}</strong
          ><span>包含准备与清理阶段</span>
        </article>
        <article>
          <small>等待任务</small
          ><strong
            >{{ state.counts.waiting_roots }} /
            {{ state.limits.waiting_roots }}</strong
          ><span>尚未开始执行</span>
        </article>
        <article>
          <small>模型请求</small
          ><strong
            >{{ state.counts.active_models }} /
            {{ state.limits.model_requests }}</strong
          ><span>主任务与子任务共用</span>
        </article>
        <article>
          <small>等待模型额度</small
          ><strong>{{ state.counts.waiting_models }}</strong
          ><span>尚未派发的请求</span>
        </article>
      </div>
      <p v-if="state.counts.preparing_admissions" class="muted">
        {{ state.counts.preparing_admissions }}
        个请求正在检查接收条件；以返回的任务身份确认接收。
      </p>
      <section
        v-for="group in [
          { title: '执行中的任务', entries: state.active },
          { title: '等待中的任务', entries: state.queued },
        ]"
        :key="group.title"
        class="panel"
      >
        <h2>{{ group.title }}</h2>
        <p v-if="!group.entries.length" class="muted">当前没有任务。</p>
        <div v-else class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>项目 / 对话</th>
                <th>执行位置</th>
                <th>状态</th>
                <th>接收时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="entry in group.entries" :key="entry.run_id">
                <td>
                  {{ project(entry)
                  }}<small class="mono">{{ entry.session_id }}</small
                  ><small class="mono">{{ entry.run_id }}</small>
                </td>
                <td>{{ target(entry) }}</td>
                <td>
                  {{ labels[entry.dispatch_state]
                  }}<small v-if="entry.position !== null"
                    >等待顺序 {{ entry.position }}</small
                  >
                </td>
                <td>
                  {{
                    new Date(entry.admitted_at * 1000).toLocaleString("zh-CN")
                  }}
                </td>
                <td>
                  <div class="button-row">
                    <button @click="open(entry)">
                      {{
                        entry.execution.kind === "evaluation"
                          ? "打开评测"
                          : "打开对话"
                      }}</button
                    ><button
                      :disabled="
                        stopping[entry.run_id] ||
                        entry.dispatch_state === 'finishing'
                      "
                      @click="stop(entry)"
                    >
                      {{
                        entry.dispatch_state === "queued"
                          ? "取消排队"
                          : "停止任务"
                      }}
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <p class="muted">
        正在直接使用源目录时，新副本需要等源目录空闲后再创建。同一对话保持串行；停止会等待工具收尾。服务重启后未完成对话任务停驻，需进入原对话检查后继续。评测不会自动重跑，需在评测页面检查已保留的结果。
      </p>
    </template>
    <p v-else class="empty-state">正在读取任务队列…</p>
  </section>
</template>
