<script setup lang="ts">
import { computed } from "vue";
import type { UsageLedger } from "../api/types";
const props = defineProps<{ ledger: UsageLedger }>();
const runs = computed(() => {
  const rows = new Map<
    string,
    {
      run: string;
      tokens: number | null;
      calls: number;
      incomplete: number;
      failed: number;
    }
  >();
  for (const call of props.ledger.calls) {
    const row = rows.get(call.run_id) || {
      run: call.run_id,
      tokens: null,
      calls: 0,
      incomplete: 0,
      failed: 0,
    };
    row.calls++;
    if (call.usage_source === "reported") {
      for (const key of ["input_tokens", "output_tokens"] as const) {
        const value = call.usage[key];
        if (value !== undefined) row.tokens = (row.tokens ?? 0) + value;
      }
    }
    if (!call.usage_complete) row.incomplete++;
    if (call.status === "failed") row.failed++;
    rows.set(call.run_id, row);
  }
  return [...rows.values()];
});
const ranked = computed(() =>
  runs.value
    .filter((row) => row.tokens !== null)
    .sort((a, b) => b.tokens! - a.tokens! || a.run.localeCompare(b.run))
    .slice(0, 5),
);
const failures = computed(() =>
  runs.value
    .filter((row) => row.failed >= 2)
    .sort((a, b) => b.failed - a.failed || a.run.localeCompare(b.run)),
);
const unranked = computed(
  () => runs.value.filter((row) => row.tokens === null).length,
);
const summaries = computed(
  () =>
    props.ledger.calls.filter((call) => call.kind === "context_summary").length,
);
</script>
<template>
  <section class="panel usage-insights">
    <h2>消耗与失败观察</h2>
    <p>
      摘要调用 {{ summaries }} / 全部尝试 {{ ledger.calls.length }} · 用量不完整
      {{ ledger.incomplete_attempts }} 次
    </p>
    <h3>已报告消耗最高的运行</h3>
    <p class="muted">
      按独立 Run 的已报告输入与输出 Token 合计排序，最多显示 5
      项。子任务独立列出；未报告的用量可能影响排名。统计覆盖所选范围全部历史。
    </p>
    <div v-if="ranked.length" class="table-scroll">
      <table class="usage-ranking">
        <thead>
          <tr>
            <th>运行身份</th>
            <th>已报告 Token</th>
            <th>调用尝试</th>
            <th>不完整调用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in ranked" :key="row.run">
            <td class="mono">{{ row.run }}</td>
            <td>{{ row.tokens?.toLocaleString("zh-CN") }}</td>
            <td>{{ row.calls }}</td>
            <td>{{ row.incomplete }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="muted">尚无可排序的已报告 Token。</p>
    <p v-if="unranked" class="muted">
      另有 {{ unranked }} 个运行没有已报告总量，未放入排名。
    </p>
    <details class="usage-failures" v-if="failures.length">
      <summary>多次调用失败的运行（{{ failures.length }}）</summary>
      <ul>
        <li v-for="row in failures" :key="row.run">
          <code>{{ row.run }}</code
          >：{{ row.failed }} 次失败 / {{ row.calls }} 次尝试
        </li>
      </ul>
      <p class="muted">
        按调用尝试的失败终态统计，取消不计入。这里只显示失败记录，不据此判断任务最终是否完成。
      </p>
    </details>
  </section>
</template>
