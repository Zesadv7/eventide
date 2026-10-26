<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import {
  interactionsApi,
  type QuestionAnswer,
  type QuestionProjection,
  type QuestionRecord,
  type QuestionStatus,
} from "../api/interactions";
const props = defineProps<{
  runId: string;
  runStatus?: string;
  refreshKey?: string | number;
}>();
const detail = ref<QuestionProjection | null>(null),
  error = ref(""),
  notice = ref(""),
  sending = ref(false),
  loading = ref(false),
  picked = ref<Record<string, number>>({}),
  drafts = ref<Record<string, string>>({});
let version = 0,
  scope = 0;
async function load() {
  const current = ++version,
    id = props.runId;
  if (!id) return;
  loading.value = true;
  try {
    const value = await interactionsApi.listQuestions(id);
    if (current === version) {
      detail.value = value;
      error.value = "";
    }
  } catch (e) {
    if (current === version)
      error.value = e instanceof Error ? e.message : "提问记录读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}
const waitingQuestions = computed(() =>
  (detail.value?.questions || []).filter((q) => q.status === "waiting"),
);
function itemKey(question: QuestionRecord, index: number): string {
  return `${question.question_id}:${index}`;
}
function entry(question: QuestionRecord, index: number): QuestionAnswer | null {
  const item = question.questions[index];
  const chosen = picked.value[itemKey(question, index)];
  const text = (drafts.value[itemKey(question, index)] || "").trim();
  if (chosen != null)
    return { choice: item.choices[chosen] ?? null, text: text || null };
  if (text) return { choice: null, text };
  return null;
}
const waitingSet = computed(() => waitingQuestions.value[0] ?? null);
const complete = computed(() => {
  const target = waitingSet.value;
  if (!target) return false;
  return target.questions.every((_, index) => entry(target, index));
});
async function submit() {
  const id = props.runId,
    currentScope = scope,
    target = waitingSet.value;
  if (!id || !target || sending.value || !complete.value) return;
  sending.value = true;
  error.value = "";
  notice.value = "";
  try {
    const answers = target.questions
      .map((_, index) => entry(target, index))
      .filter((a): a is QuestionAnswer => a !== null);
    const receipt = await interactionsApi.answerQuestion(
      id,
      target.question_id,
      answers,
      target.revision,
    );
    if (currentScope !== scope) return;
    if (receipt.status === "late") {
      notice.value =
        "该运行已结束：回答已存档为迟到记录，不会重开或继续任何运行。";
    } else if (receipt.duplicate) {
      notice.value = "该组问题已回答过：本次重复提交未产生新副作用。";
    } else {
      notice.value = "已回答；答案会作为本次提问的工具结果返回原运行。";
    }
    picked.value = {};
    drafts.value = {};
    await load();
  } catch (e) {
    if (currentScope === scope)
      error.value = e instanceof Error ? e.message : "回答发送失败";
  } finally {
    if (currentScope === scope) sending.value = false;
  }
}
const statusLabel: Record<QuestionStatus, string> = {
  waiting: "等待你的回答",
  answered: "回答已接收",
  late: "迟到回答 · 不会续开运行",
  closed: "已关闭 · 不再接受回答",
  expired: "已过期 · 运行不再活跃",
};
watch(
  () => props.runId,
  () => {
    scope++;
    version++;
    detail.value = null;
    picked.value = {};
    drafts.value = {};
    notice.value = "";
    error.value = "";
    sending.value = false;
    void load();
  },
  { immediate: true },
);
watch(() => props.refreshKey, load);
onUnmounted(() => {
  version++;
  scope++;
});
</script>
<template>
  <details v-if="runId" class="run-questions" open>
    <summary>
      运行中提问
      <span v-if="detail?.waiting" class="badge"
        >{{ detail.waiting }} 组待回答</span
      >
    </summary>
    <p class="muted">
      服务重启后原问题保留为关闭或过期记录，本阶段不会自动重建等待。
    </p>
    <p>
      提问与回答都记录在原运行的事件日志里；回答只会作为工具结果进入原运行，
      不代表任何工具授权；用户沉默不会被当作同意。
    </p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="detail && !detail.questions.length" class="muted">
      本次运行暂无提问记录。
    </p>
    <article v-for="item in detail?.questions || []" :key="item.question_id">
      <header>
        <span class="badge" :class="{ live: item.status === 'waiting' }">{{
          statusLabel[item.status]
        }}</span>
        <small class="muted mono">{{ item.question_id }}</small>
      </header>
      <ol class="question-list">
        <li v-for="(q, index) in item.questions" :key="index">
          <p class="question-text">{{ q.text }}</p>
          <div
            v-if="q.choices.length && item.status === 'waiting'"
            class="choice-row"
          >
            <button
              v-for="(choice, ci) in q.choices"
              :key="choice"
              :class="{
                selected: picked[itemKey(item, index)] === ci,
              }"
              :disabled="sending"
              :aria-pressed="picked[itemKey(item, index)] === ci"
              @click="
                picked[itemKey(item, index)] === ci
                  ? delete picked[itemKey(item, index)]
                  : (picked[itemKey(item, index)] = ci)
              "
            >
              {{ choice }}
            </button>
          </div>
          <p v-else-if="q.choices.length" class="muted">
            选项：{{ q.choices.join(" / ") }}
          </p>
          <label
            v-if="item.status === 'waiting' && q.allow_free_text"
            class="free-text"
          >
            <span class="muted">自由输入（可选）</span>
            <textarea
              v-model="drafts[itemKey(item, index)]"
              rows="2"
              maxlength="4000"
              :aria-label="`问题${index + 1}的自由输入`"
              :disabled="sending"
              :placeholder="
                q.choices.length ? '也可以直接输入答案' : '请输入答案'
              "
            ></textarea>
          </label>
          <p
            v-if="item.answers && item.answers[index]"
            class="muted answer-line"
          >
            你的回答：
            <strong>{{
              [item.answers[index].choice, item.answers[index].text]
                .filter(Boolean)
                .join("；")
            }}</strong>
          </p>
        </li>
      </ol>
      <p v-if="item.closed_reason" class="muted">
        关闭原因：{{ item.closed_reason }}
      </p>
    </article>
    <div v-if="waitingQuestions.length" class="button-row">
      <button class="primary" :disabled="sending || !complete" @click="submit">
        {{ sending ? "提交中…" : "提交回答" }}
      </button>
      <button :disabled="loading" @click="load">刷新提问</button>
    </div>
  </details>
</template>
<style scoped>
.run-questions article {
  border-top: 1px solid var(--border, #ddd);
  padding-top: 0.5rem;
  margin-top: 0.5rem;
}
.question-list {
  margin: 0.5rem 0 0;
  padding-left: 1.25rem;
}
.question-text {
  margin: 0.25rem 0;
}
.choice-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin: 0.25rem 0;
}
.choice-row button.selected {
  outline: 2px solid currentColor;
}
.free-text textarea {
  width: 100%;
}
.answer-line {
  margin: 0.25rem 0;
}
.badge.live {
  font-weight: 600;
}
</style>
