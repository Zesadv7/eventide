<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { UsageLedger } from "../api/types";
import { useWorkbench } from "../stores/workbench";
const store = useWorkbench(),
  scope = ref<"sessions" | "runs">("sessions"),
  ledger = ref<UsageLedger | null>(null),
  error = ref(""),
  loading = ref(false);
let version = 0,
  timer: ReturnType<typeof setInterval> | undefined;
const identity = computed(() =>
  scope.value === "sessions" ? store.selectedSession : store.currentRun?.run_id,
);
async function load() {
  const current = ++version;
  error.value = "";
  if (!identity.value) {
    ledger.value = null;
    return;
  }
  loading.value = true;
  try {
    const result = await api.usage(identity.value, scope.value);
    if (current === version) ledger.value = result;
  } catch (e) {
    if (current === version)
      error.value = e instanceof Error ? e.message : "读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}
watch(
  [identity, scope],
  () => {
    ledger.value = null;
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
        :class="{ selected: scope === 'sessions' }"
        @click="scope = 'sessions'"
      >
        当前会话全部历史</button
      ><button :class="{ selected: scope === 'runs' }" @click="scope = 'runs'">
        最近运行
      </button>
    </div>
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
      <div class="alert">
        <span
          >费用、共享预算和输入组成分析仍待后端接入。未报价显示为未知，未返回用量不会当作零消费。</span
        >
      </div>
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
      <section class="panel">
        <h2>请求明细</h2>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>开始时间／身份</th>
                <th>用途／模型</th>
                <th>结果</th>
                <th>输入</th>
                <th>输出</th>
                <th>依据</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="call in ledger.calls"
                :key="`${call.run_id}:${call.attempt_id}`"
              >
                <td>
                  {{ when(call.started_at)
                  }}<small class="mono">{{ call.attempt_id }}</small>
                </td>
                <td>
                  {{ call.kind }}<small>{{ call.model || "未记录模型" }}</small>
                </td>
                <td>{{ call.status }}</td>
                <td>{{ tokens(call.usage.input_tokens) }}</td>
                <td>{{ tokens(call.usage.output_tokens) }}</td>
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
            </tbody>
          </table>
        </div>
      </section></template
    >
  </section>
</template>
