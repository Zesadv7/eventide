<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  evaluationsApi,
  evaluationRequestState,
  observeEvaluations,
  type EvaluationQuote,
  type EvaluationDetail,
  type EvaluationReport,
  type EvaluationSplit,
  type EvaluationSuite,
} from "../api/evaluations";
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
const suites = ref<EvaluationSuite[]>([]),
  split = ref<EvaluationSplit>("development");
const mode = ref<"offline" | "live">("offline"),
  quote = ref<EvaluationQuote | null>(null),
  quoting = ref(false),
  confirmed = ref(false);
let quoteRevision = 0;
async function loadQuote() {
  const revision = ++quoteRevision,
    owner = project.value,
    selection = split.value;
  quoting.value = true;
  quote.value = null;
  confirmed.value = false;
  error.value = "";
  try {
    const result = await evaluationsApi.quote(owner, selection);
    if (
      revision === quoteRevision &&
      owner === project.value &&
      selection === split.value
    )
      quote.value = result;
  } catch (reason) {
    if (revision === quoteRevision) error.value = String(reason);
  } finally {
    if (revision === quoteRevision) quoting.value = false;
  }
}
watch([project, split, mode], () => {
  quoteRevision++;
  quote.value = null;
  confirmed.value = false;
  quoting.value = false;
});
const entries = ref<EvaluationDetail[]>([]),
  detail = ref<EvaluationDetail | null>(null),
  report = ref<EvaluationReport | null>(null);
const listError = ref(""),
  detailError = ref(""),
  suiteError = ref("");
const selected = ref(""),
  error = ref(""),
  reportError = ref(""),
  loading = ref(false),
  moreLoading = ref(false),
  hasMore = ref(false),
  stopping = ref(false),
  reportLoading = ref(false);
const requestState = ref(evaluationRequestState(project.value));
const suite = computed(() =>
  suites.value.find((s) => s.id === "engineering-v1"),
);
const busy = computed(() =>
  entries.value.some(
    (entry) => entry.status === "queued" || entry.status === "running",
  ),
);
const pending = computed(() => requestState.value.pending);
const canStart = computed(
  () =>
    !!project.value &&
    !requestState.value.active &&
    (!!pending.value ||
      (!!suite.value &&
        !busy.value &&
        (mode.value === "offline" ||
          (!!quote.value && confirmed.value && !quoting.value)))),
);
const activeDetail = computed(
  () => detail.value?.status === "queued" || detail.value?.status === "running",
);
const splitLabels = {
  development: "开发集",
  holdout: "留出集",
  all: "全部任务",
};
const statusLabels: Record<string, string> = {
  queued: "排队中",
  starting: "准备执行",
  running: "执行中",
  finishing: "收尾中",
  completed: "执行完毕",
  failed: "执行失败",
  interrupted: "已中断",
  pending: "未运行",
  timed_out: "超时",
  unknown: "结果未知",
};
let mounted = true,
  projectRevision = 0,
  detailRevision = 0,
  listFlight = -1,
  detailFlight = "",
  timer: ReturnType<typeof setInterval> | undefined;
const observedReceipts = new Map<string, string>();
const stopObserving = observeEvaluations(() => syncRequest());

function syncRequest() {
  requestState.value = evaluationRequestState(project.value);
  const receipt = requestState.value.receipt;
  if (
    receipt &&
    observedReceipts.get(project.value) !== receipt.evaluation_id
  ) {
    observedReceipts.set(project.value, receipt.evaluation_id);
    choose(receipt.evaluation_id);
    void loadList();
  }
}
function queryId() {
  return typeof route.query.evaluation === "string"
    ? route.query.evaluation
    : "";
}
function choose(id: string) {
  if (!mounted) return;
  if (selected.value !== id) {
    selected.value = id;
    detail.value = null;
    report.value = null;
    reportError.value = "";
    reportLoading.value = false;
    stopping.value = false;
    detailRevision++;
  }
  if (queryId() !== id)
    void router.replace({
      query: { ...route.query, evaluation: id || undefined },
    });
  void loadDetail();
}
async function loadList() {
  const current = projectRevision,
    owner = project.value;
  if (!owner || listFlight === current) return;
  listFlight = current;
  loading.value = true;
  try {
    const values = await evaluationsApi.list(owner);
    if (!mounted || current !== projectRevision) return;
    const ids = new Set(values.map((value) => value.evaluation_id));
    entries.value = [
      ...values,
      ...entries.value.filter((value) => !ids.has(value.evaluation_id)),
    ];
    if (entries.value.length <= 20) hasMore.value = values.length === 20;
    listError.value = "";
    if (!selected.value && values[0]) choose(values[0].evaluation_id);
  } catch (e) {
    if (mounted && current === projectRevision)
      listError.value = e instanceof Error ? e.message : "评测历史读取失败";
  } finally {
    if (current === projectRevision) {
      loading.value = false;
      listFlight = -1;
    }
  }
}
async function loadMore() {
  if (moreLoading.value || !entries.value.length) return;
  const current = projectRevision,
    owner = project.value;
  moreLoading.value = true;
  try {
    const values = await evaluationsApi.list(
      owner,
      entries.value.at(-1)!.evaluation_id,
    );
    if (!mounted || current !== projectRevision) return;
    const ids = new Set(entries.value.map((value) => value.evaluation_id));
    entries.value.push(
      ...values.filter((value) => !ids.has(value.evaluation_id)),
    );
    hasMore.value = values.length === 20;
  } catch (e) {
    if (mounted && current === projectRevision)
      listError.value = e instanceof Error ? e.message : "较早评测读取失败";
  } finally {
    if (current === projectRevision) moreLoading.value = false;
  }
}
async function loadDetail() {
  const current = projectRevision,
    revision = detailRevision,
    id = selected.value,
    owner = project.value;
  const flight = `${current}:${revision}:${id}`;
  if (!owner || !id || detailFlight === flight) return;
  detailFlight = flight;
  try {
    const value = await evaluationsApi.detail(id);
    if (
      !mounted ||
      current !== projectRevision ||
      revision !== detailRevision ||
      id !== selected.value
    )
      return;
    if (value.project_id !== owner)
      throw new Error("该评测不属于当前项目，请先选择所属项目。");
    detail.value = value;
    detailError.value = "";
  } catch (e) {
    if (mounted && current === projectRevision && revision === detailRevision)
      detailError.value = e instanceof Error ? e.message : "评测详情读取失败";
  } finally {
    if (detailFlight === flight) detailFlight = "";
  }
}
function refresh() {
  void loadList();
  void loadDetail();
}
watch(
  project,
  (value, previous) => {
    projectRevision++;
    detailRevision++;
    entries.value = [];
    detail.value = null;
    report.value = null;
    error.value = "";
    listError.value = "";
    detailError.value = "";
    reportError.value = "";
    hasMore.value = false;
    loading.value = false;
    moreLoading.value = false;
    stopping.value = false;
    reportLoading.value = false;
    selected.value = previous ? "" : queryId();
    if (previous && queryId())
      void router.replace({ query: { ...route.query, evaluation: undefined } });
    requestState.value = evaluationRequestState(value);
    if (value) {
      const receipt = requestState.value.receipt;
      if (!selected.value && receipt) selected.value = receipt.evaluation_id;
      if (receipt) observedReceipts.set(value, receipt.evaluation_id);
      refresh();
    }
  },
  { immediate: true },
);
watch(
  () => route.query.evaluation,
  () => {
    if (project.value && queryId() !== selected.value) choose(queryId());
  },
);
onMounted(async () => {
  timer = setInterval(refresh, 3000);
  try {
    const values = await evaluationsApi.suites();
    if (mounted) suites.value = values;
  } catch (e) {
    if (mounted)
      suiteError.value = e instanceof Error ? e.message : "任务集读取失败";
  }
});
onUnmounted(() => {
  mounted = false;
  quoteRevision++;
  projectRevision++;
  detailRevision++;
  clearInterval(timer);
  stopObserving();
});

async function start() {
  if (!canStart.value) return;
  const owner = project.value,
    current = projectRevision;
  const body = pending.value || {
    suite_id: "engineering-v1" as const,
    suite_sha256: suite.value!.suite_sha256,
    split: split.value,
    mode: mode.value,
    ...(mode.value === "live"
      ? { quote_hash: quote.value!.quote_hash, confirmed: true }
      : {}),
  };
  error.value = "";
  try {
    const receipt = await evaluationsApi.create(owner, body);
    if (mounted && current === projectRevision) {
      quote.value = null;
      confirmed.value = false;
      choose(receipt.evaluation_id);
      await loadList();
    }
  } catch (e) {
    if (mounted && current === projectRevision)
      error.value =
        e instanceof Error ? e.message : "评测请求未确认，请重试原请求";
  } finally {
    if (mounted && current === projectRevision)
      requestState.value = evaluationRequestState(owner);
  }
}
async function stop() {
  if (!detail.value || !activeDetail.value || stopping.value) return;
  const id = detail.value.evaluation_id,
    current = projectRevision,
    revision = detailRevision;
  stopping.value = true;
  error.value = "";
  try {
    const value = await evaluationsApi.cancel(id);
    if (mounted && current === projectRevision && revision === detailRevision) {
      // Invalidate any read that started before the mutation returned.
      detailRevision++;
      reportLoading.value = false;
      report.value = null;
      detail.value = value;
      void loadList();
    }
  } catch (e) {
    if (mounted && current === projectRevision && revision === detailRevision)
      error.value =
        e instanceof Error ? e.message : "停止结果未确认，请重试停止";
  } finally {
    if (mounted && current === projectRevision && id === selected.value)
      stopping.value = false;
  }
}
async function readReport() {
  if (!detail.value?.report_available || reportLoading.value) return;
  const id = selected.value,
    current = projectRevision,
    revision = detailRevision;
  reportLoading.value = true;
  reportError.value = "";
  try {
    const value = await evaluationsApi.report(id);
    if (mounted && current === projectRevision && revision === detailRevision)
      report.value = value;
  } catch (e) {
    if (mounted && current === projectRevision && revision === detailRevision)
      reportError.value = e instanceof Error ? e.message : "报告读取失败";
  } finally {
    if (mounted && current === projectRevision && revision === detailRevision)
      reportLoading.value = false;
  }
}
function downloadReport() {
  if (!report.value) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(report.value, null, 2)], {
      type: "application/json",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `evaluation-${selected.value}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>

<template>
  <section class="page evaluations-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">EVALUATIONS</p>
        <h1>工程评测</h1>
        <p class="muted">
          {{ projectName }} ·
          离线验证机制，真实模型实验使用独立验收和共享预算。离线机制结果不代表真实模型成功率。
        </p>
      </div>
      <button :disabled="loading" @click="refresh">刷新评测</button>
    </header>
    <p
      v-for="message in [error, listError, detailError, suiteError].filter(
        Boolean,
      )"
      :key="message"
      class="inline-error"
      role="alert"
    >
      {{ message }}
    </p>
    <section class="panel" aria-label="发起评测">
      <h2>冻结工程任务集</h2>
      <p>
        每批只执行一次，使用独立临时项目，不修改所选项目源码。离线使用预设响应；真实模型调用计入项目和每日预算，任务与普通对话共用服务额度。
      </p>
      <p v-if="suite" class="muted">
        {{ suite.description }} · 版本 {{ suite.version }} ·
        {{ suite.total }} 个任务
      </p>
      <form @submit.prevent="start">
        <label for="evaluation-mode">评测模式</label>
        <select
          id="evaluation-mode"
          :value="pending?.mode || mode"
          @change="
            mode = ($event.target as HTMLSelectElement).value as
              'offline' | 'live'
          "
          :disabled="requestState.active || !!pending"
        >
          <option value="offline">离线机制自检</option>
          <option v-if="suite?.supported_modes.includes('live')" value="live">
            真实模型实验（产生费用）
          </option>
        </select>
        <label for="evaluation-split">任务划分</label
        ><select
          id="evaluation-split"
          :value="pending?.split || split"
          :disabled="requestState.active || !!pending"
          @change="
            split = ($event.target as HTMLSelectElement)
              .value as EvaluationSplit
          "
        >
          <option value="development">
            开发集{{ suite ? ` · ${suite.splits.development} 题` : "" }}
          </option>
          <option value="holdout">
            留出集{{ suite ? ` · ${suite.splits.holdout} 题` : "" }}
          </option>
          <option value="all">
            全部任务{{ suite ? ` · ${suite.total} 题` : "" }}
          </option></select
        ><button class="primary" :disabled="!canStart">
          {{
            requestState.active
              ? "正在接收…"
              : pending
                ? "重试原评测请求"
                : mode === "live"
                  ? "运行一次真实模型实验"
                  : "运行一次离线自检"
          }}
        </button>
      </form>
      <section
        v-if="mode === 'live' && !pending"
        class="live-quote"
        aria-label="真实评测规划估算"
      >
        <button
          :disabled="quoting || requestState.active || !project"
          @click="loadQuote"
        >
          {{ quoting ? "正在读取估算…" : "读取模型与费用估算" }}
        </button>
        <template v-if="quote">
          <p>
            {{ quote.provider }} / {{ quote.model }} · {{ quote.case_count }} 题
          </p>
          <p>
            规划输入 {{ quote.planning_tokens.input_tokens }} Token，输出
            {{ quote.planning_tokens.output_tokens }} Token。
          </p>
          <p v-for="(estimate, currency) in quote.estimates" :key="currency">
            {{ currency }} {{ estimate.known_amount }}
            {{ estimate.complete ? "规划估算" : "已知费用部分（不完整）" }}
          </p>
          <p v-if="!quote.complete">
            费用估算不完整或未知，不表示免费。可先在用量页配置价格。
          </p>
          <p class="muted">{{ quote.assumptions }}</p>
          <label
            ><input
              type="checkbox"
              v-model="confirmed"
            />我已核对本批模型、规划估算及未知费用，确认发起真实调用</label
          >
        </template>
      </section>
      <p
        v-if="pending && !requestState.active"
        role="status"
        class="pending-note"
      >
        上次评测接收尚未确认（{{
          pending.mode === "live" ? "真实模型" : "离线"
        }}）。重试会使用原确认、任务集指纹和{{
          splitLabels[pending.split]
        }}，不会另开一批。也可从历史核对后台状态。
      </p>
      <p v-else-if="busy" class="muted">
        本项目已有排队或执行中的评测；结束后可发起新批次。
      </p>
      <p class="muted">
        离线模式只检查执行机制；参考真实模型留出结果调整策略后，必须更换留出题。每题最多12步、20,000已观测Token、300秒，停止会等待工具及证据保存收尾。
      </p>
    </section>
    <section class="panel" aria-label="评测历史">
      <h2>项目评测历史</h2>
      <p v-if="!entries.length" class="muted">
        {{ loading ? "正在读取…" : "当前项目还没有评测。" }}
      </p>
      <div v-else class="evaluation-history">
        <button
          v-for="entry in entries"
          :key="entry.evaluation_id"
          :class="{ selected: selected === entry.evaluation_id }"
          @click="choose(entry.evaluation_id)"
        >
          <strong
            >{{
              statusLabels[entry.dispatch_state || entry.status] || entry.status
            }}
            · {{ splitLabels[entry.split] }} ·
            {{ entry.mode === "live" ? "真实模型" : "离线机制" }}</strong
          ><span
            >{{ entry.attempted }} / {{ entry.total }} 已尝试 ·
            {{ entry.passed }} 通过</span
          ><small class="mono">{{ entry.evaluation_id }}</small>
        </button>
      </div>
      <button v-if="hasMore" :disabled="moreLoading" @click="loadMore">
        加载较早评测
      </button>
    </section>
    <section
      v-if="detail"
      class="panel evaluation-detail"
      aria-label="评测运行详情"
    >
      <div class="page-heading">
        <div>
          <h2>
            {{
              statusLabels[detail.dispatch_state || detail.status] ||
              detail.status
            }}
          </h2>
          <p class="mono">{{ detail.evaluation_id }}</p>
          <p>
            {{
              detail.mode === "live"
                ? "真实模型实验 · 共享主服务预算"
                : "离线机制自检 · 无付费调用"
            }}
          </p>
          <RouterLink
            v-if="detail.mode === 'live'"
            :to="{ path: '/usage', query: { run: detail.run_id } }"
            >查看实际调用用量</RouterLink
          >
        </div>
        <button
          v-if="activeDetail"
          :disabled="stopping || detail.dispatch_state === 'finishing'"
          @click="stop"
        >
          {{
            stopping
              ? "等待停止收尾…"
              : detail.status === "queued"
                ? "取消评测排队"
                : "停止评测"
          }}
        </button>
      </div>
      <p v-if="detail.queue_position !== null">
        等待顺序：{{ detail.queue_position }}
      </p>
      <p v-if="detail.current_case">当前用例：{{ detail.current_case }}</p>
      <p v-if="detail.error" class="inline-error">{{ detail.error }}</p>
      <div class="stat-grid">
        <article>
          <small>已尝试 / 总任务</small
          ><strong>{{ detail.attempted }} / {{ detail.total }}</strong>
        </article>
        <article>
          <small>{{
            detail.mode === "live" ? "独立验收通过" : "机制通过"
          }}</small
          ><strong>{{ detail.passed }}</strong>
        </article>
        <article>
          <small>未通过</small><strong>{{ detail.failed }}</strong>
        </article>
        <article>
          <small>未尝试</small><strong>{{ detail.remaining }}</strong>
        </article>
      </div>
      <p class="muted">
        批次执行完毕不代表每题通过。中断与未运行题保持原状态；服务重启不会自动重跑，重新发起会创建新批次。离线脚本用量不是实际模型账单。
      </p>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>用例 / 划分</th>
              <th>状态</th>
              <th>验收</th>
              <th>耗时</th>
              <th>失败与停止原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in detail.cases" :key="item.id">
              <td>
                {{ item.id
                }}<small
                  >{{ item.category }} ·
                  {{
                    splitLabels[item.split as EvaluationSplit] || item.split
                  }}</small
                >
              </td>
              <td>{{ statusLabels[item.status] || item.status }}</td>
              <td>
                {{
                  item.passed === true
                    ? "通过"
                    : item.passed === false
                      ? "未通过"
                      : "未确认"
                }}
              </td>
              <td>
                {{
                  item.duration_ms === null
                    ? "未知"
                    : `${item.duration_ms.toLocaleString("zh-CN")} ms`
                }}
              </td>
              <td>{{ item.reasons.join("；") || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <details>
        <summary>固定任务与事件身份</summary>
        <p>
          任务集 {{ detail.suite_id }} · 版本 {{ detail.suite_version }} ·
          {{ splitLabels[detail.split] }}
        </p>
        <p class="mono">SHA-256 {{ detail.suite_sha256 }}</p>
        <p class="mono">
          Run {{ detail.run_id }}<br />Session {{ detail.session_id }}
        </p>
      </details>
      <div class="button-row">
        <button
          :disabled="!detail.report_available || reportLoading"
          @click="readReport"
        >
          {{ reportLoading ? "读取报告…" : "读取完整报告" }}</button
        ><button v-if="report" @click="downloadReport">下载报告 JSON</button>
      </div>
      <p v-if="!detail.report_available" class="muted">
        报告尚不可用，运行状态仍以已保存事件为准。
      </p>
      <p v-if="reportError" class="inline-error" role="alert">
        {{ reportError }}
      </p>
      <details v-if="report" class="report">
        <summary>
          完整{{ report.billing_applicable ? "模型实验" : "离线" }}报告 · schema
          {{ report.schema_version }}
        </summary>
        <p class="muted">
          这是读取时的报告快照，可再次读取以更新。保留原始失败、未知用量与逐题证据。{{
            report.billing_applicable
              ? "模型用量来自主服务唯一调用账本，费用未知部分单独保留。"
              : "scripted 结果不能作为真实模型质量或费用结论。"
          }}
        </p>
        <pre>{{ JSON.stringify(report, null, 2) }}</pre>
      </details>
    </section>
    <p v-else-if="selected" class="muted">正在读取所选评测详情…</p>
  </section>
</template>

<style scoped>
.live-quote {
  overflow-wrap: anywhere;
  margin-top: 1rem;
}

.evaluations-page {
  overflow-wrap: anywhere;
}
.evaluations-page .page-heading > button {
  flex-shrink: 0;
}
form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
.evaluation-history {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.evaluation-history button {
  text-align: left;
  min-width: 0;
  display: grid;
  gap: 8px;
}
.evaluation-history button.selected {
  border-color: var(--accent);
  background: var(--soft);
}
.evaluation-history span {
  font-size: 13px;
}
.evaluation-history small {
  color: var(--muted);
}
.evaluation-detail table {
  min-width: 660px;
}
.evaluation-detail td small {
  display: block;
  color: var(--muted);
  margin-top: 7px;
}
.pending-note {
  color: var(--warning);
}
.report pre {
  max-height: 520px;
  overflow: auto;
}
</style>
