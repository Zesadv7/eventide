<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { budgetsApi, type Costs, type Scope } from "../api/budgets";
import type { UsageLedger } from "../api/types";
import { useWorkbench } from "../stores/workbench";
import BudgetPanel from "../components/BudgetPanel.vue";
import CostSummary from "../components/CostSummary.vue";
import PriceEditor from "../components/PriceEditor.vue";
import InputComposition from "../components/InputComposition.vue";
import UsageInsights from "../components/UsageInsights.vue";
const route = useRoute();
const budgetPanel = ref<InstanceType<typeof BudgetPanel> | null>(null);
function selectScope(value: Scope["kind"]) {
  if (budgetPanel.value?.confirmLeave() !== false) scope.value = value;
}
function selectDay(event: Event) {
  const input = event.target as HTMLInputElement;
  if (budgetPanel.value?.confirmLeave() !== false) day.value = input.value;
  else input.value = day.value;
}
const linkedRun = computed(() =>
  typeof route?.query.run === "string" ? route.query.run : "",
);
const linkedDelivery = computed(() =>
  typeof route?.query.delivery === "string" ? route.query.delivery : "",
);
const store = useWorkbench(),
  scope = ref<Scope["kind"]>(
    linkedDelivery.value ? "delivery" : linkedRun.value ? "run" : "session",
  ),
  day = ref(new Date().toISOString().slice(0, 10)),
  ledger = ref<UsageLedger | null>(null),
  costs = ref<Costs | null>(null),
  error = ref(""),
  loading = ref(false);
let version = 0,
  loadingKey = "",
  timer: ReturnType<typeof setInterval> | undefined;
const identity = computed(() => {
  if (scope.value === "session") return store.selectedSession;
  if (scope.value === "run") return linkedRun.value || store.currentRun?.run_id;
  if (scope.value === "delivery") return linkedDelivery.value;
  if (scope.value === "day") return day.value;
  return store.workspaces.find(
    (item) => item.workspace_id === store.selectedWorkspace,
  )?.project_id;
});
const selectedScope = computed<Scope | null>(() =>
  identity.value ? { kind: scope.value, identity: identity.value } : null,
);
async function load() {
  const key = `${scope.value}:${identity.value || ""}`;
  if (loading.value && loadingKey === key) return;
  loadingKey = key;
  const current = ++version;
  error.value = "";
  if (!identity.value) {
    ledger.value = null;
    costs.value = null;
    loading.value = false;
    return;
  }
  loading.value = true;
  try {
    const result = await budgetsApi.costs(selectedScope.value!);
    if (current === version) {
      costs.value = result;
      ledger.value = result.usage;
    }
  } catch (e) {
    if (current === version)
      error.value = e instanceof Error ? e.message : "读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}
watch([linkedRun, linkedDelivery], ([run, delivery]) => {
  if (delivery) scope.value = "delivery";
  else if (run) scope.value = "run";
  else if (scope.value === "delivery") scope.value = "session";
});
watch(
  [identity, scope],
  () => {
    ledger.value = null;
    costs.value = null;
    void load();
  },
  { immediate: true },
);
onMounted(() => {
  timer = setInterval(() => void load(), 4000);
});
onUnmounted(() => {
  version++;
  clearInterval(timer);
});
function tokens(value: unknown) {
  return typeof value === "number" ? value.toLocaleString("zh-CN") : "未报告";
}
function duration(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? `${value.toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ms`
    : "未记录";
}
function when(value: number) {
  return new Date(value * 1000).toLocaleString("zh-CN");
}
const grouped = computed(() => {
  const result: Record<
    string,
    {
      calls: number;
      input: number | null;
      output: number | null;
      unknown: number;
    }
  > = {};
  for (const call of ledger.value?.calls || []) {
    const row = (result[call.kind] ??= {
      calls: 0,
      input: null,
      output: null,
      unknown: 0,
    });
    row.calls++;
    if (call.usage_source === "reported") {
      if (call.usage.input_tokens !== undefined)
        row.input = (row.input ?? 0) + call.usage.input_tokens;
      if (call.usage.output_tokens !== undefined)
        row.output = (row.output ?? 0) + call.usage.output_tokens;
    }
    if (!call.usage_complete) row.unknown++;
  }
  return result;
});
</script>
<template>
  <section class="page usage-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">USAGE / EVIDENCE</p>
        <h1>用量与成本</h1>
        <p class="muted">
          按真实模型请求尝试去重，包含子任务；缓存与推理细项不再次加进总量。
        </p>
      </div>
      <button @click="load" :disabled="loading">刷新</button>
    </header>
    <div class="segmented">
      <button
        v-if="linkedDelivery"
        :class="{ selected: scope === 'delivery' }"
        @click="selectScope('delivery')"
      >
        指定交付全部轮次
      </button>
      <button
        :class="{ selected: scope === 'session' }"
        @click="selectScope('session')"
      >
        当前会话全部历史</button
      ><button
        :class="{ selected: scope === 'run' }"
        @click="selectScope('run')"
      >
        {{ linkedRun ? "指定运行" : "最近运行" }}
      </button>
      <button
        :class="{ selected: scope === 'project' }"
        @click="selectScope('project')"
      >
        当前项目全部对话
      </button>
      <button
        :class="{ selected: scope === 'day' }"
        @click="selectScope('day')"
      >
        每日（UTC）
      </button>
    </div>
    <p v-if="scope === 'delivery'" class="muted">
      交付
      {{
        linkedDelivery
      }}：包含所有轮次、摘要和子任务的真实尝试；关闭或重启交付不会清零累计用量。
    </p>
    <label v-if="scope === 'day'" class="button-row"
      >统计日期（UTC）<input
        :value="day"
        @change="selectDay"
        type="date"
        required
    /></label>
    <p
      v-if="linkedRun && scope === 'run'"
      class="muted"
      style="overflow-wrap: anywhere"
    >
      运行：{{ linkedRun }}
    </p>
    <p v-if="!identity" class="empty-state">
      先选择一个项目对话，运行后即可查看用量。
    </p>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <template v-if="ledger"
      ><div class="stat-grid">
        <article>
          <small>已报告输入</small
          ><strong>{{ tokens(ledger.reported_totals.input_tokens) }}</strong
          ><span>Token</span>
        </article>
        <article>
          <small>已报告输出</small
          ><strong>{{ tokens(ledger.reported_totals.output_tokens) }}</strong
          ><span>Token</span>
        </article>
        <article>
          <small>调用尝试</small><strong>{{ ledger.attempt_count }}</strong
          ><span>非运行摘要重复计数</span>
        </article>
        <article>
          <small>用量未知</small><strong>{{ ledger.unknown_attempts }}</strong
          ><span>{{ ledger.complete ? "统计完整" : "统计不完整" }}</span>
        </article>
      </div>
      <CostSummary v-if="costs" :costs="costs" />
      <BudgetPanel
        ref="budgetPanel"
        v-if="selectedScope && scope !== 'session'"
        :scope="selectedScope" />
      <p v-else class="muted">
        会话统计包含 Continue
        与子任务。共享预算可在“当前项目全部对话”、“最近运行”或“每日”范围管理。
      </p>
      <section class="panel">
        <h2>调用分类</h2>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>用途</th>
                <th>调用数</th>
                <th>已报告输入</th>
                <th>已报告输出</th>
                <th>不完整调用</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, name) in grouped" :key="name">
                <td>{{ name }}</td>
                <td>{{ row.calls }}</td>
                <td>{{ tokens(row.input) }}</td>
                <td>{{ tokens(row.output) }}</td>
                <td>{{ row.unknown }}</td>
              </tr>
              <tr v-if="!ledger.calls.length">
                <td colspan="5">尚无模型调用事实。</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>缓存与推理细项</h2>
        <dl class="facts">
          <template
            v-for="(label, key) in {
              cache_read_input_tokens: '缓存读取',
              cache_creation_input_tokens: '缓存写入',
              reasoning_tokens: '推理 Token',
            }"
            :key="key"
            ><dt>{{ label }}</dt>
            <dd>{{ tokens(ledger.reported_totals[key]) }}</dd></template
          >
        </dl>
        <p class="muted">
          这些字段的包含关系取决于供应商；缺失表示未报告。缓存命中可能减少费用和延迟，不代表输入
          Token 必然减少。
        </p>
      </section>
      <UsageInsights :ledger="ledger" />
      <section class="panel">
        <h2>请求明细</h2>
        <p class="muted">
          耗时来自每次实际请求的记录，不含排队等待；历史缺失值不补算。
        </p>
        <div class="table-scroll">
          <table class="call-details">
            <thead>
              <tr>
                <th>开始时间／身份</th>
                <th>用途／供应商／模型</th>
                <th>结果／结束原因</th>
                <th>请求耗时</th>
                <th>输入</th>
                <th>输出</th>
                <th>依据</th>
              </tr>
            </thead>
            <tbody>
              <template
                v-for="call in ledger.calls"
                :key="`${call.run_id}:${call.attempt_id}`"
              >
                <tr>
                  <td>
                    {{ when(call.started_at)
                    }}<small class="mono">{{ call.attempt_id }}</small>
                  </td>
                  <td>
                    {{ call.kind
                    }}<small>{{ call.provider || "未记录供应商" }}</small
                    ><small>{{ call.model || "未记录模型" }}</small>
                  </td>
                  <td>
                    {{ call.status
                    }}<small>{{ call.stop_reason || "未记录结束原因" }}</small>
                  </td>
                  <td>{{ duration(call.duration_ms) }}</td>
                  <td>{{ tokens(call.usage.input_tokens) }}</td>
                  <td>
                    {{
                      call.usage_basis === "input_only"
                        ? "不适用（仅输入）"
                        : tokens(call.usage.output_tokens)
                    }}
                  </td>
                  <td>
                    {{
                      call.usage_source === "reported"
                        ? "供应商报告"
                        : call.usage_source === "estimated"
                          ? "本地估算"
                          : "未知"
                    }}<small v-if="!call.usage_complete">不完整</small>
                  </td>
                </tr>
                <tr>
                  <td colspan="7">
                    <InputComposition :value="call.input_composition" />
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div></section
    ></template>
    <PriceEditor />
  </section>
</template>
