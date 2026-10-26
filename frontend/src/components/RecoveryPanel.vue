<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { api } from "../api/client";
import { recoveryApi, type RecoveryView } from "../api/recovery";

const props = defineProps<{ runId: string }>();
const view = ref<RecoveryView | null>(null),
  loading = ref(false),
  acting = ref(false),
  continuedRun = ref<string | null>(null),
  error = ref("");
let revision = 0,
  requestVersion = 0,
  mounted = true;
watch(
  () => props.runId,
  () => {
    revision++;
    view.value = null;
    loading.value = false;
    acting.value = false;
    continuedRun.value = null;
    error.value = "";
    void load();
  },
);
onMounted(() => void load());
onUnmounted(() => {
  mounted = false;
});
async function load() {
  const current = ++requestVersion,
    scope = revision,
    identity = props.runId;
  loading.value = true;
  try {
    const value = await recoveryApi.detail(identity);
    // Late responses from a previous run must never land on another run's view.
    if (mounted && current === requestVersion && scope === revision) {
      view.value = value;
      error.value = "";
    }
  } catch (e) {
    if (mounted && current === requestVersion && scope === revision)
      error.value = e instanceof Error ? e.message : "恢复详情读取失败";
  } finally {
    if (current === requestVersion && scope === revision) loading.value = false;
  }
}
async function stopRun() {
  if (acting.value) return;
  const scope = revision,
    run = props.runId;
  acting.value = true;
  try {
    await api.cancel(run);
    if (mounted && scope === revision) await load();
  } catch (e) {
    if (mounted && scope === revision)
      error.value = e instanceof Error ? e.message : "停止请求未确认";
  } finally {
    if (mounted && scope === revision) acting.value = false;
  }
}
async function continueRun() {
  if (acting.value || !view.value) return;
  const scope = revision,
    run = props.runId,
    session = view.value.session_id;
  acting.value = true;
  try {
    const receipt = await api.continueFrom(session, run);
    if (mounted && scope === revision) continuedRun.value = receipt.run_id;
    if (mounted && scope === revision) await load();
  } catch (e) {
    if (mounted && scope === revision)
      error.value =
        e instanceof Error ? e.message : "继续结果未确认，请保留原请求重试";
  } finally {
    if (mounted && scope === revision) acting.value = false;
  }
}
async function reconcileOutcomes() {
  if (acting.value || !view.value) return;
  const scope = revision,
    run = props.runId;
  acting.value = true;
  try {
    await recoveryApi.reconcile(run);
    if (mounted && scope === revision) await load();
  } catch (e) {
    if (mounted && scope === revision)
      error.value =
        e instanceof Error ? e.message : "核查结果未确认，请保留原请求重试";
  } finally {
    if (mounted && scope === revision) acting.value = false;
  }
}
const checkReasons: Record<string, string> = {
  expected_file_state_present:
    "当前文件符合原操作预期，目标外工作区与写前证据一致。",
  target_hidden_in_index: "目标被Git索引标志隐藏，当前无法安全核清。",
  target_not_git_visible: "目标不在Git可见范围内，当前无法安全核清。",
  current_matches_before:
    "当前仍是原内容；这不能证明操作从未执行，仍需你判断。",
  file_state_changed: "当前文件既不符合原内容，也不符合预期内容。",
  workspace_context_changed: "目标之外的工作区已变化，无法建立原执行边界。",
  workspace_changed_during_check: "核查期间工作区发生变化，未确认结果。",
  missing_prepared_workspace_evidence:
    "旧记录缺少写前工作区证据，无法自动核清。",
  original_call_evidence_mismatch: "原调用证据不完整或不匹配，无法核清。",
  unsupported_or_missing_file_evidence: "该工具没有可核对的内置文件恢复证据。",
  evidence_unavailable: "证据当前不可用。",
  file_operation_lifecycle_changed: "文件操作后来已撤销或进入其他处理阶段。",
};
const kindLabels: Record<string, string> = {
  model_retry_wait: "模型重试等待",
  auto_recovered: "已领取自动续跑任务",
  recovery_blocked: "自动恢复受阻",
  service_shutdown: "服务关闭后等待恢复",
  unknown_outcome: "工具结果未知，已停驻",
  file_outcome_verified: "预期文件结果已核清，原中断记录保留",
  user_cancel: "已由用户停止",
  budget: "预算停驻",
  repeated_failure: "连续失败停驻",
  host_restart: "服务重启后停驻",
  waiting_user: "等待用户处理",
  completed: "已正常完成",
  failure: "已失败",
  queued: "排队中",
  running: "执行中",
  interrupted: "已中断",
};
const statusLabels: Record<string, string> = {
  completed: "已完成",
  failed: "已失败",
  interrupted: "已中断",
  queued: "排队中",
  running: "执行中",
  waiting_for_user: "等待用户",
};
function time(value: number | null | undefined) {
  if (!value) return "未记录";
  return new Date(value * 1000).toLocaleString();
}
</script>
<template>
  <section class="recovery-panel">
    <p class="muted">
      恢复详情来自已保存事实。点击“只读核查”会读取实际文件并保存核查证据，不重放原工具、不执行待核查的命令，也不自动授权；停止与继续仍由服务端重新复核。
    </p>
    <button :disabled="loading" @click="load">
      {{ loading ? "读取中…" : view ? "刷新恢复详情" : "读取恢复详情" }}
    </button>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="continuedRun" role="status">
      已接收继续请求：<RouterLink :to="`/runs/${continuedRun}/recovery`"
        >查看续做运行</RouterLink
      >。
    </p>
    <template v-if="view">
      <p class="mono">
        投影版本 {{ view.projection_version }} · 事件高水位
        {{ view.high_water }} · 状态：{{
          statusLabels[view.status] || view.status
        }}
      </p>
      <p
        v-if="!view.recovery.history_available"
        role="alert"
        class="history-note"
      >
        该运行创建于持久恢复能力之前：没有冻结的恢复策略记录，本页只展示历史事实，不推断它是否可恢复。
      </p>
      <h3>当前处于什么状态</h3>
      <p>
        {{ kindLabels[view.stop.kind] || view.stop.kind
        }}<template v-if="view.stop.reason">（{{ view.stop.reason }}）</template
        ><template v-if="view.stop.error">：{{ view.stop.error }}</template>
      </p>
      <p v-if="view.model_retry" class="mono" data-testid="model-retry">
        模型请求等待自动重试：第
        {{ view.model_retry.attempt ?? "?" }} 次尝试，约
        {{ time(view.model_retry.retry_at) }}
        前后自动重试；服务重启后会重新核对已保存事实，不保证沿用原倒计时。
      </p>
      <h3>已记录的执行进度</h3>
      <p>
        {{ view.summary.steps }} 个模型步骤 ·
        {{ view.summary.tool_calls }} 次工具调用<template
          v-if="view.summary.checkpoint_seq !== null"
        >
          · 工作区检查点序号 {{ view.summary.checkpoint_seq }}</template
        >
      </p>
      <template v-if="view.unknown_tools.length">
        <h3>结果未知的工具</h3>
        <ul data-testid="unknown-tools">
          <li v-for="tool in view.unknown_tools" :key="String(tool.call_id)">
            {{ tool.name || "未知工具" }}（{{ tool.call_id }}）{{
              tool.error ? `：${tool.error}` : ""
            }}
            — 尚未核清，不能直接继续。
          </li>
        </ul>
      </template>
      <div
        v-if="view.unknown_tools.length && view.is_terminal"
        class="button-row"
      >
        <button
          data-testid="reconcile-outcomes"
          :disabled="acting"
          @click="reconcileOutcomes"
        >
          {{ acting ? "处理中…" : "只读核查文件结果" }}
        </button>
      </div>
      <template v-if="view.outcome_checks?.length">
        <h3>文件结果核查证据</h3>
        <ul data-testid="outcome-checks">
          <li
            v-for="(check, index) in view.outcome_checks"
            :key="`${check.unknown_event_id}:${index}`"
          >
            {{ check.name }}（{{ check.call_id }}）：{{
              check.status === "verified_after"
                ? "预期文件状态已核清"
                : "仍需判断"
            }}。
            {{ checkReasons[check.reason] || check.reason }}
            <span class="muted">核查时间 {{ time(check.at) }}</span>
            <span v-if="check.detail">{{ check.detail }}</span>
            <span v-if="check.observed_file?.sha256" class="mono"
              >观察版本 {{ check.observed_file.sha256 }}</span
            >
          </li>
        </ul>
        <p class="muted">
          核查只证明观察到的文件状态；不会改写原未知记录，也不证明作者身份、测试通过或授权后续操作。
        </p>
      </template>
      <template v-if="view.pending_approvals.length">
        <h3>等待审批</h3>
        <ul>
          <li v-for="item in view.pending_approvals" :key="item.approval_id">
            {{ item.approval_id }}
          </li>
        </ul>
      </template>
      <h3>恢复关系</h3>
      <p v-if="view.continuation_of">
        本运行是 {{ view.continuation_of }} 的后继：
        <RouterLink :to="`/runs/${view.continuation_of}/recovery`"
          >查看原运行恢复详情</RouterLink
        >
        ·
        <RouterLink :to="`/runs/${view.continuation_of}/diagnostics`"
          >原运行故障报告</RouterLink
        >
      </p>
      <p v-else class="muted">没有记录表明本运行是其他运行的后继。</p>
      <template v-if="view.recovery.successor">
        <p data-testid="successor">
          本运行已领取唯一后继 {{ view.recovery.successor_run_id }}
          <template
            v-if="
              view.recovery.successor.admitted &&
              view.recovery.successor.current
            "
            >，当前状态：{{
              statusLabels[view.recovery.successor.current.status || ""] ||
              view.recovery.successor.current.status
            }}（实时读取，非本页快照）</template
          ><template v-else
            >，后继尚未被服务接收（可能等待重启重派或已被阻塞）</template
          >。
          <RouterLink :to="`/runs/${view.recovery.successor_run_id}/recovery`"
            >查看后继恢复详情</RouterLink
          >
          ·
          <RouterLink
            :to="`/runs/${view.recovery.successor_run_id}/diagnostics`"
            >后继故障报告</RouterLink
          >
        </p>
      </template>
      <p v-else class="muted">本运行没有被自动续跑领取的后继。</p>
      <template v-if="view.recovery.blocked.length">
        <h3>被阻塞的恢复</h3>
        <ul>
          <li
            v-for="item in view.recovery.blocked"
            :key="String(item.resumed_as) + String(item.at)"
          >
            {{ item.reason }}（原计划后继 {{ item.resumed_as }}）·
            {{ time(item.at) }}
          </li>
        </ul>
      </template>
      <h3>可执行的操作</h3>
      <p class="muted">
        点击后仍会核对源码、模型配置、预算和原运行编号；这里展示的是可检查的操作入口，不是恢复成功保证。结果未知时不会提供直接继续。
      </p>
      <div class="button-row">
        <button
          v-if="view.available_actions.stop.available"
          data-testid="stop-run"
          :disabled="acting"
          @click="stopRun"
        >
          停止当前运行
        </button>
        <button
          v-if="view.available_actions.continue.available"
          data-testid="continue-run"
          :disabled="acting"
          @click="continueRun"
        >
          Continue · 检查并继续
        </button>
        <span
          v-if="
            !view.available_actions.stop.available &&
            !view.available_actions.continue.available
          "
          class="muted"
          >当前事实下没有可用操作{{
            view.available_actions.continue.reason
              ? `（${view.available_actions.continue.reason}）`
              : ""
          }}。</span
        >
      </div>
      <details>
        <summary>说明与限制</summary>
        <ul class="muted">
          <li>
            本页内容绑定读取时的事件高水位（{{
              view.high_water
            }}）；刷新后才会纳入新事实。
          </li>
          <li>
            后继状态为实时读取；其余内容是历史事实投影，两者在页面上分开呈现。
          </li>
          <li v-for="(note, index) in view.notes" :key="index">{{ note }}</li>
        </ul>
      </details>
    </template>
  </section>
</template>
<style scoped>
.recovery-panel {
  overflow-wrap: anywhere;
  min-width: 0;
}
.recovery-panel a {
  margin-right: 0.6rem;
}
.recovery-panel h3 {
  margin: 0.8rem 0 0.3rem;
}
.button-row {
  display: flex;
  gap: 0.6rem;
  align-items: center;
  flex-wrap: wrap;
  margin: 0.4rem 0;
}
.history-note {
  border: 1px solid currentColor;
  padding: 0.4rem 0.6rem;
}
ul {
  padding-left: 1.2rem;
  margin: 0.3rem 0;
}
</style>
