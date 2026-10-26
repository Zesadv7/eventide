<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import {
  contextSummariesApi,
  type SummaryDetail,
  type SummaryEntry,
  type SummaryEvidence,
} from "../api/contextSummaries";
const props = defineProps<{ sessionId: string; checkpointId: number }>();
const data = ref<SummaryDetail | null>(null),
  evidence = ref<SummaryEvidence | null>(null),
  error = ref(""),
  loading = ref(false),
  evidenceLoading = ref(false);
let generation = 0,
  evidenceGeneration = 0;
const sections: Record<string, string> = {
  hard_constraints: "硬约束",
  facts: "有引文的事实",
  unverified: "尚未证实的判断",
  checks: "检查结果",
  failures: "失败与否定",
  next_steps: "后续事项",
};
const statusLabels: Record<string, string> = {
  passed: "通过",
  failed: "失败",
  unknown: "未知",
};
function entries(key: string): (SummaryEntry | string)[] {
  return (data.value?.structured?.[key] as (SummaryEntry | string)[]) || [];
}
async function load() {
  const current = ++generation;
  evidenceGeneration++;
  data.value = null;
  evidence.value = null;
  error.value = "";
  loading.value = true;
  evidenceLoading.value = false;
  try {
    const result = await contextSummariesApi.detail(
      props.sessionId,
      props.checkpointId,
    );
    if (current === generation) data.value = result;
  } catch (e) {
    if (current === generation)
      error.value = e instanceof Error ? e.message : "摘要读取失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}
async function source(index: number, offset = 0) {
  const current = ++evidenceGeneration,
    scope = generation;
  evidenceLoading.value = true;
  error.value = "";
  try {
    const result = await contextSummariesApi.message(
      props.sessionId,
      props.checkpointId,
      index,
      offset,
    );
    if (current === evidenceGeneration && scope === generation)
      evidence.value = result;
  } catch (e) {
    if (current === evidenceGeneration && scope === generation)
      error.value = e instanceof Error ? e.message : "原始证据读取失败";
  } finally {
    if (current === evidenceGeneration && scope === generation)
      evidenceLoading.value = false;
  }
}
watch(() => [props.sessionId, props.checkpointId], load, { immediate: true });
onUnmounted(() => {
  generation++;
  evidenceGeneration++;
});
</script>
<template>
  <article class="summary-version">
    <p v-if="loading">正在读取摘要…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="error" @click="load">重新读取摘要</button>
    <template v-if="data">
      <h4>版本 {{ data.id }} · {{ data.model }}</h4>
      <p>
        覆盖历史至事件 {{ data.covered_seq }} · 摘要
        {{ data.summary_characters }} 字符
      </p>
      <p>
        {{ data.source_verified ? "历史内容指纹已核对" : data.source_problem }}
      </p>
      <p v-if="data.input_verification === 'matched'">
        重建输入与当时摘要请求的指纹一致。
      </p>
      <p v-else>
        缺少原输入记录或投影规则已变化；下方只代表当前可重建内容，不能断言与当时输入完全一致。
      </p>
      <p>引文存在不等于结论正确；这份记录也不能证明每次模型请求都使用了它。</p>
      <p v-if="data.citations_problem" role="alert">
        引文校验失败：{{ data.citations_problem }}
      </p>
      <template v-if="data.structured">
        <h5>目标</h5>
        <p>{{ data.structured.goal }}</p>
        <section v-for="(label, key) in sections" :key="key">
          <h5>{{ label }}</h5>
          <p v-if="!entries(key).length" class="muted">本节未记录。</p>
          <div v-for="(entry, i) in entries(key)" :key="i">
            <p v-if="typeof entry === 'string'">{{ entry }}</p>
            <template v-else>
              <p>
                <span v-if="entry.status"
                  >[{{ statusLabels[entry.status] || entry.status }}] </span
                >{{ entry.text }}
              </p>
              <blockquote v-for="(ref, j) in entry.src || []" :key="j">
                {{ ref.quote }}
                <button :disabled="evidenceLoading" @click="source(ref.msg)">
                  查看原始消息 {{ ref.msg }}
                </button>
              </blockquote>
            </template>
          </div>
        </section>
      </template>
      <template v-else
        ><p>原始摘要文本（旧格式或尚未通过结构化核对）：</p>
        <pre>{{ data.summary }}</pre>
      </template>
      <p v-if="data.summary_truncated">
        摘要过长，此处只显示前 24,000 字符，省略部分未展示。
      </p>
      <details>
        <summary>版本指纹</summary>
        <p>摘要 {{ data.summary_hash }}</p>
        <p>历史 {{ data.source_digest }}</p>
      </details>
      <section v-if="evidence" class="summary-evidence">
        <h5>
          原始消息 {{ evidence.message_index }} · 字符 {{ evidence.offset }}–{{
            evidence.offset + evidence.content.length
          }}
          / {{ evidence.total_characters }}
        </h5>
        <p>这是摘要使用的消息投影；长工具日志可能在生成摘要前已经裁剪。</p>
        <pre>{{ evidence.content }}</pre>
        <button
          v-if="evidence.next_offset !== null"
          :disabled="evidenceLoading"
          @click="source(evidence.message_index, evidence.next_offset)"
        >
          下一段原始消息
        </button>
      </section>
    </template>
  </article>
</template>
<style scoped>
.summary-version {
  min-width: 0;
  overflow-wrap: anywhere;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
blockquote {
  margin: 8px 0;
  border-left: 2px solid var(--line);
  padding-left: 8px;
}
h5 {
  margin: 16px 0 6px;
}
</style>
