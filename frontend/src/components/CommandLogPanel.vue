<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import {
  commandLogsApi,
  type CommandLogPage,
  type CommandLogReference,
} from "../api/commandLogs";
const props = defineProps<{ log: CommandLogReference }>();
const stream = ref<"stdout" | "stderr">("stdout"),
  page = ref<CommandLogPage | null>(null),
  loading = ref(false),
  error = ref(""),
  offset = ref(0);
const PAGE = 4000;
let revision = 0,
  mounted = true;
const stateLabels: Record<string, string> = {
  complete: "采集完成",
  interrupted: "采集中断",
  quota_exceeded: "超过日志配额，此处不是全部输出",
  write_failed: "日志写入失败",
  not_started: "命令未实际执行",
  redaction_unavailable: "密钥过长，未保存完整日志",
};
watch(
  () => props.log.artifact_id,
  () => {
    revision++;
    loading.value = false;
    stream.value = "stdout";
    page.value = null;
    error.value = "";
    offset.value = 0;
  },
);
function guard(current: number, identity: CommandLogReference): boolean {
  return (
    mounted &&
    current === revision &&
    identity.artifact_id === props.log.artifact_id
  );
}
async function load(target: number) {
  const current = ++revision,
    identity = props.log;
  loading.value = true;
  error.value = "";
  try {
    const value = await commandLogsApi.page(
      identity.run_id,
      identity.call_id,
      stream.value,
      target,
      PAGE,
    );
    if (guard(current, identity)) {
      page.value = value;
      offset.value = target;
    }
  } catch (e) {
    if (guard(current, identity))
      error.value = e instanceof Error ? e.message : "命令日志读取失败";
  } finally {
    if (guard(current, identity)) loading.value = false;
  }
}
function switchStream(next: "stdout" | "stderr") {
  if (stream.value === next) return;
  stream.value = next;
  page.value = null;
  offset.value = 0;
  void load(0);
}
onUnmounted(() => {
  mounted = false;
  revision++;
});
</script>
<template>
  <section class="command-log-panel" aria-label="完整命令日志">
    <p class="muted">
      模型预览被截断时，完整脱敏输出按
      Run/调用身份保存；此处与模型看到的内容分开，超配额会明确标记。
      覆盖范围：本机普通 Shell 与 Skill 脚本输出；容器/沙箱执行未接入此采集，
      也不会自动降级为宿主采集，此类调用不提供完整日志。
    </p>
    <p v-if="log.available === false" role="alert">
      日志记录失败：{{ log.availability }}（{{ log.detail }}）
    </p>
    <div v-else class="stream-tabs" role="tablist">
      <button
        role="tab"
        :aria-selected="stream === 'stdout'"
        :class="{ active: stream === 'stdout' }"
        @click="switchStream('stdout')"
      >
        stdout
      </button>
      <button
        role="tab"
        :aria-selected="stream === 'stderr'"
        :class="{ active: stream === 'stderr' }"
        @click="switchStream('stderr')"
      >
        stderr
      </button>
      <button :disabled="loading" @click="load(offset)">
        {{ loading ? "读取中…" : page ? "刷新本页" : "读取完整日志" }}
      </button>
    </div>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="page && !page.available" role="alert">
      日志不可用：{{ page.availability || "未知原因"
      }}<template v-if="page.reason">（{{ page.reason }}）</template>
    </p>
    <template v-if="page && page.available">
      <p class="muted facts">
        字符 {{ page.start }}–{{ page.end }} / {{ page.total_chars }} · 保存
        {{ page.stored_bytes }} 字节 / 收到
        {{ page.received_bytes ?? "未知" }} 字节 ·
        {{ stateLabels[page.capture_state] || page.capture_state
        }}<template v-if="page.encoding_loss"> · 存在无法解码字节</template>
      </p>
      <pre>{{ page.text }}</pre>
      <div class="paging">
        <button
          v-if="(page.end ?? 0) < (page.total_chars ?? 0)"
          @click="load(page.next_offset ?? 0)"
        >
          下一页
        </button>
        <button v-if="offset > 0" @click="load(Math.max(0, offset - PAGE))">
          上一页
        </button>
        <span v-if="(page.end ?? 0) >= (page.total_chars ?? 0)" class="muted"
          >已到末尾</span
        >
      </div>
    </template>
  </section>
</template>
<style scoped>
.command-log-panel {
  margin: 0.35rem 0;
}
.stream-tabs {
  display: flex;
  gap: 0.4rem;
  margin: 0.3rem 0;
}
.command-log-panel pre {
  max-height: 18rem;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.paging {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
.facts {
  overflow-wrap: anywhere;
}
</style>
