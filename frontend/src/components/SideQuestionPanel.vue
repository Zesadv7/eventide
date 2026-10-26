<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client";
import { sideQuestionsApi, type SideQuestionView } from "../api/sideQuestions";

const props = defineProps<{
  runId: string;
  sessionId?: string;
  runStatus?: string;
  refreshKey?: string | number;
}>();

const questions = ref<SideQuestionView[]>([]),
  draft = ref(""),
  error = ref(""),
  notice = ref(""),
  asking = ref(false),
  loading = ref(false);
const returning = ref<Record<string, boolean>>({});
const stopping = ref<Record<string, boolean>>({});
let version = 0,
  scope = 0,
  poller: ReturnType<typeof setInterval> | null = null;

const busy = computed(() =>
  questions.value.some((q) =>
    ["queued", "running"].includes(q.side_run_status),
  ),
);

async function load() {
  const current = ++version,
    id = props.runId;
  if (!id) return;
  loading.value = true;
  try {
    const value = await sideQuestionsApi.list(id);
    if (current === version) questions.value = value.side_questions;
  } catch (e) {
    if (current === version)
      error.value = e instanceof Error ? e.message : "旁枝提问读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}

function ensurePolling() {
  if (busy.value && poller === null) {
    poller = setInterval(() => void load(), 2000);
  } else if (!busy.value && poller !== null) {
    clearInterval(poller);
    poller = null;
  }
}

async function ask() {
  const session = props.sessionId,
    text = draft.value.trim(),
    runId = props.runId,
    currentScope = scope;
  if (!session || !text || asking.value) return;
  asking.value = true;
  error.value = "";
  notice.value = "";
  try {
    await sideQuestionsApi.ask(session, text, runId);
    if (currentScope !== scope) return;
    if (draft.value.trim() === text) draft.value = "";
    notice.value = "旁枝调查已开始；主任务不受影响。";
    await load();
  } catch (e) {
    if (currentScope === scope)
      error.value = e instanceof Error ? e.message : "旁枝提问发送失败";
  } finally {
    if (currentScope === scope) {
      asking.value = false;
      ensurePolling();
    }
  }
}

async function stop(item: SideQuestionView) {
  const currentScope = scope;
  if (stopping.value[item.side_question_id]) return;
  stopping.value = {
    ...stopping.value,
    [item.side_question_id]: true,
  };
  try {
    await api.cancel(item.side_run_id);
    if (currentScope === scope) await load();
  } catch (e) {
    if (currentScope === scope)
      error.value = e instanceof Error ? e.message : "旁枝停止失败";
  } finally {
    if (currentScope === scope) {
      const next = { ...stopping.value };
      delete next[item.side_question_id];
      stopping.value = next;
      ensurePolling();
    }
  }
}

async function returnAnswer(item: SideQuestionView) {
  const currentScope = scope;
  if (item.returned || returning.value[item.side_question_id]) return;
  if (!item.answer.trim()) {
    error.value = "该旁枝还没有可回传的答案。";
    return;
  }
  returning.value = {
    ...returning.value,
    [item.side_question_id]: true,
  };
  error.value = "";
  try {
    // Bind the receipt to the exact answer text the user just saw.
    const receipt = await sideQuestionsApi.return(
      item.side_question_id,
      item.answer,
    );
    if (currentScope !== scope) return;
    if (receipt.body_truncated) {
      notice.value =
        "答案过长，只有部分答案写回主任务，正文已标注截断；不会自动续跑。";
    } else if (receipt.status === "late") {
      notice.value = "主任务已结束：答案已存档为迟到回传，不会自动续跑。";
    } else if (receipt.already_returned) {
      notice.value = "该答案此前已回传，未重复注入。";
    } else {
      notice.value = "答案已回传；将在主任务下一个模型边界消费一次。";
    }
    await load();
  } catch (e) {
    if (currentScope === scope)
      error.value = e instanceof Error ? e.message : "回传失败";
  } finally {
    if (currentScope === scope) {
      const next = { ...returning.value };
      delete next[item.side_question_id];
      returning.value = next;
      ensurePolling();
    }
  }
}

watch(
  () => props.runId,
  () => {
    scope++;
    version++;
    questions.value = [];
    draft.value = "";
    notice.value = "";
    error.value = "";
    asking.value = false;
    returning.value = {};
    stopping.value = {};
    void load();
  },
  { immediate: true },
);
watch(
  () => props.refreshKey,
  () => void load(),
);
watch(questions, ensurePolling);
onUnmounted(() => {
  version++;
  scope++;
  if (poller !== null) clearInterval(poller);
});
</script>
<template>
  <details v-if="runId" class="run-side-questions">
    <summary>
      旁枝提问
      <span v-if="busy" class="badge">调查中</span>
      <span v-if="questions.length && !busy" class="badge"
        >{{ questions.length }} 条</span
      >
    </summary>
    <p>
      旁枝只读调查快照：主任务继续运行，不被取消或排队；只有你点“回传答案”才会写回主任务，迟到答案不会自动续跑。
    </p>
    <label class="side-question-editor">
      <textarea
        v-model="draft"
        rows="2"
        maxlength="8000"
        aria-label="旁枝提问内容"
        placeholder="针对主任务当前进度提问（只读调查）"
      ></textarea>
    </label>
    <div class="button-row">
      <button
        class="primary"
        :disabled="asking || !draft.trim() || !sessionId"
        @click="ask"
      >
        {{ asking ? "创建中…" : "发起旁枝提问" }}
      </button>
      <button :disabled="loading" @click="load">刷新</button>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!questions.length && !loading" class="muted">
      本次运行暂无旁枝提问。
    </p>
    <article v-for="item in questions" :key="item.side_question_id">
      <p>
        <span class="badge">{{ item.side_run_status }}</span>
        <span v-if="item.returned" class="badge">
          已回传{{ item.supplement?.status === "late" ? "（迟到）" : "" }}
        </span>
        <span class="muted">
          快照 {{ item.snapshot_id.slice(0, 12) }} · 高水位
          {{ item.source_high_water }}
        </span>
      </p>
      <p v-if="item.files_captured_at" class="muted">
        文件取自 {{ item.source_execution?.kind || "source" }}，读取时间
        {{ item.files_captured_at }}；文件快照与事件截止位置分别记录。
      </p>
      <p class="side-question-text">{{ item.question }}</p>
      <p
        v-if="item.source_context?.evidence_events !== undefined"
        class="muted"
      >
        证据：当前会话截止此位置共
        {{ item.source_context.evidence_events }} 个事件；{{
          item.source_context.tool_activity || "无工具活动"
        }}。
      </p>
      <p v-if="item.cleanup_error" role="alert">
        快照清理失败：{{ item.cleanup_error }}
      </p>
      <p v-if="item.answer" class="side-answer">{{ item.answer }}</p>
      <p
        v-else-if="['failed', 'interrupted'].includes(item.side_run_status)"
        class="muted"
      >
        旁枝未产生答案。
      </p>
      <div class="button-row">
        <button
          v-if="['queued', 'running'].includes(item.side_run_status)"
          :disabled="stopping[item.side_question_id]"
          @click="stop(item)"
        >
          {{ stopping[item.side_question_id] ? "停止中…" : "停止旁枝" }}
        </button>
        <button
          v-if="item.side_run_status === 'completed' && !item.returned"
          class="primary"
          :disabled="returning[item.side_question_id]"
          @click="returnAnswer(item)"
        >
          {{
            returning[item.side_question_id] ? "回传中…" : "回传答案到主任务"
          }}
        </button>
        <span v-if="item.returned && item.supplement" class="muted">
          {{ item.supplement.consumed ? "已在主任务消费" : "等待主任务消费" }}
        </span>
      </div>
    </article>
  </details>
</template>
<style scoped>
.side-question-editor textarea,
.side-question-text,
.side-answer {
  width: 100%;
  white-space: pre-wrap;
}
</style>
