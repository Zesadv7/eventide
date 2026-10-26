<script setup lang="ts">
import { computed } from "vue";
import FileOperationPanel from "./FileOperationPanel.vue";
import SourceEvidencePanel from "./SourceEvidencePanel.vue";
import type { SourceEvidence } from "../api/sourceEvidence";
interface RetrievalEvidence {
  workspace_id: string;
  index_generation?: number | null;
  embedding_fingerprint?: string | null;
  mode?: string;
  diagnostics?: string[];
  hits: {
    file: string;
    start_line: number;
    end_line: number;
    indexed_content_hash: string;
    freshness: string;
  }[];
}
const freshness: Record<string, string> = {
  fresh: "核对时一致",
  changed: "源码已变化，旧正文不可作为证据",
  deleted: "文件已删除",
  unsafe: "路径不安全，未读取",
  unreadable: "文件无法读取",
  too_large: "文件超过核对上限",
};
const props = defineProps<{
  tools: Record<string, unknown>;
  count?: number;
  active?: boolean;
  sessionId?: string;
}>();
const entries = computed(() =>
  Object.entries(props.tools).map(([id, value]) => {
    const data = (
      value && typeof value === "object" ? value : { output: value }
    ) as Record<string, unknown>;
    const metadata = data.metadata as
      | { retrieval?: RetrievalEvidence; source_evidence?: SourceEvidence }
      | undefined;
    const builtinSource = ["read_file", "search_text"].includes(
      String(data.name || data.tool_name),
    );
    return {
      id,
      data,
      retrieval: metadata?.retrieval,
      source: builtinSource ? metadata?.source_evidence : undefined,
    };
  }),
);
function status(data: Record<string, unknown>) {
  if (data.status === "unknown" || data.status === "outcome_unknown")
    return "结果待确认";
  if (data.is_error || data.status === "failed") return "执行失败";
  if (data.status === "completed") return "已完成";
  return String(data.status || "已记录");
}
</script>
<template>
  <details v-if="entries.length || count || sessionId" class="tool-activity">
    <summary>
      <span
        class="activity-glyph"
        :class="{ 'is-working': active }"
        aria-hidden="true"
        >✳</span
      >工具活动 <span>{{ count ?? entries.length }} 次调用</span
      ><span class="activity-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="tool-activity-list">
      <details v-for="entry in entries" :key="entry.id" class="tool-entry">
        <summary>
          <span>{{ entry.data.name || entry.data.tool_name || entry.id }}</span
          ><small>{{ status(entry.data) }}</small>
        </summary>
        <section
          v-if="entry.retrieval"
          class="retrieval-evidence"
          aria-label="检索来源"
        >
          <p>实际副本：{{ entry.retrieval.workspace_id }}</p>
          <p>
            索引代次：{{ entry.retrieval.index_generation ?? "未记录" }} ·
            模式：{{ entry.retrieval.mode || "未记录" }}
          </p>
          <p>
            配置指纹：{{ entry.retrieval.embedding_fingerprint || "未记录" }}
          </p>
          <p
            v-for="(diagnostic, index) in entry.retrieval.diagnostics || []"
            :key="index"
          >
            诊断：{{ diagnostic }}
          </p>
          <ul>
            <li v-for="(hit, index) in entry.retrieval.hits" :key="index">
              <p>
                {{ hit.file }} · 索引行 {{ hit.start_line }}–{{ hit.end_line }}
              </p>
              <p>{{ freshness[hit.freshness] || hit.freshness }}</p>
              <p>索引内容 SHA-256：{{ hit.indexed_content_hash }}</p>
            </li>
          </ul>
          <p v-if="!entry.retrieval.hits.length">
            没有命中片段，请结合索引状态和诊断判断原因。
          </p>
          <p class="muted">
            一致仅表示检查时文件未变化；关键结论仍需阅读当前源码。
          </p>
        </section>
        <p
          v-else-if="
            entry.data.name === 'search_knowledge' ||
            entry.data.tool_name === 'search_knowledge'
          "
          class="muted"
        >
          检索来源：未记录
        </p>
        <SourceEvidencePanel
          v-if="entry.source"
          :evidence="entry.source"
          :content="
            typeof entry.data.content === 'string'
              ? entry.data.content
              : undefined
          "
        />
        <p
          v-else-if="
            ['read_file', 'search_text'].includes(
              String(entry.data.name || entry.data.tool_name),
            )
          "
          class="muted"
        >
          这条历史记录没有结构化源码证据，请查看原始工具记录。
        </p>
        <details v-if="entry.retrieval || entry.source">
          <summary>原始工具记录</summary>
          <pre>{{ JSON.stringify(entry.data, null, 2) }}</pre>
        </details>
        <pre v-else>{{ JSON.stringify(entry.data, null, 2) }}</pre>
      </details>
      <p v-if="!entries.length" class="muted">
        工具明细尚未返回，可在运行证据中查看事件记录。
      </p>
      <FileOperationPanel
        v-if="sessionId"
        :key="sessionId"
        :session-id="sessionId"
        :revision="tools"
      />
    </div>
  </details>
</template>

<style scoped>
.retrieval-evidence {
  overflow-wrap: anywhere;
}
.retrieval-evidence p {
  margin: 0.35rem 0;
}
</style>
