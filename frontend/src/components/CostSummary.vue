<script setup lang="ts">
import type { Costs } from "../api/budgets";
defineProps<{ costs: Costs }>();
</script>

<template>
  <section class="panel" aria-label="费用估算">
    <h2>费用估算</h2>
    <p class="muted">
      {{ costs.complete ? "当前范围估算完整" : "当前范围估算不完整" }} ·
      {{ costs.unknown_attempts }}
      次费用未知。仅使用调用时固定的价格版本，金额保持原币种。
    </p>
    <dl class="facts">
      <template v-for="(amount, currency) in costs.known_costs" :key="currency"
        ><dt>{{ currency }} 已知费用</dt>
        <dd>{{ amount }}</dd></template
      >
      <template
        v-for="(amount, currency) in costs.estimated_usage_costs"
        :key="`estimated-${currency}`"
        ><dt>{{ currency }} 估算用量对应费用</dt>
        <dd>{{ amount }}（非供应商报告）</dd></template
      >
    </dl>
    <p
      v-if="
        !Object.keys(costs.known_costs).length &&
        !Object.keys(costs.estimated_usage_costs).length
      "
      class="muted"
    >
      费用未知，尚无可计价的调用证据。
    </p>
    <p v-if="costs.historical_unreserved_attempt_ids.length" class="muted">
      {{ costs.historical_unreserved_attempt_ids.length }}
      次历史调用尚无新预算记录；保留原用量，不补造价格或预留。
    </p>
    <details v-if="costs.latest.length">
      <summary>查看 {{ costs.latest.length }} 次调用的价格与分类依据</summary>
      <article
        v-for="item in costs.latest"
        :key="item.estimate_id"
        class="cost-call"
      >
        <h3>
          {{ item.model }} <small>{{ item.provider }}</small>
        </h3>
        <p class="mono muted">{{ item.attempt_id }} · {{ item.pricing_at }}</p>
        <p v-if="!Object.keys(item.estimates).length" class="muted">
          费用未知：没有有效价格。
        </p>
        <div
          v-for="(estimate, currency) in item.estimates"
          :key="currency"
          class="table-scroll"
        >
          <p>
            {{ currency }}：{{ estimate.known_amount ?? "未知" }} ·
            {{ estimate.complete ? "费用类别完整" : "仍有未知类别" }}
          </p>
          <table>
            <thead>
              <tr>
                <th>类别</th>
                <th>Token</th>
                <th>单价 / 每百万</th>
                <th>金额</th>
                <th>固定价格版本</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="line in estimate.lines" :key="line.category">
                <td>{{ line.category }}</td>
                <td>{{ line.quantity }}</td>
                <td>{{ line.price_per_million }}</td>
                <td>{{ line.amount }}</td>
                <td class="mono">{{ line.price_version }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="estimate.unknown_categories.length" class="muted">
            未知类别：{{ estimate.unknown_categories.join("、") }}
          </p>
        </div>
      </article>
    </details>
  </section>
</template>
