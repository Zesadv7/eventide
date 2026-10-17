<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  budgetsApi,
  type BudgetStatus,
  type Reservation,
  type Scope,
} from "../api/budgets";

import {
  useUnsavedChanges,
  confirmDiscard,
} from "../composables/useUnsavedChanges";
import PanelClose from "./PanelClose.vue";
const props = defineProps<{ scope: Scope }>();
const state = ref<BudgetStatus | null>(null),
  error = ref(""),
  message = ref("");
const enabled = ref(false),
  limit = ref<string | number>(""),
  dirty = ref(false),
  saving = ref(false),
  loading = ref(false);
const draftHash = ref(""),
  selected = ref(""),
  action = ref<"keep_unknown" | "record_known">("keep_unknown");
const actual = ref<string | number>(""),
  reason = ref("");
const canLeave = useUnsavedChanges(
  () => dirty.value || !!reason.value || !!String(actual.value),
  { guardQueryChanges: true },
);
defineExpose({ confirmLeave: canLeave });
function closeResolution() {
  if (!confirmDiscard(!!reason.value || !!String(actual.value))) return;
  selected.value = "";
  reason.value = "";
  actual.value = "";
}
let epoch = 0,
  loadingKey = "",
  timer: ReturnType<typeof setInterval> | undefined;
const scopeKey = computed(() => `${props.scope.kind}:${props.scope.identity}`);
const budget = computed(() => state.value?.budgets[0]);
const unsettled = computed(
  () =>
    state.value?.reservations.filter((item) => item.status === "unsettled") ||
    [],
);
const target = computed(() =>
  unsettled.value.find((item) => item.attempt_id === selected.value),
);
function count(value: number | null | undefined) {
  return typeof value === "number" ? value.toLocaleString("zh-CN") : "未设置";
}
function applyDraft() {
  if (!budget.value) return;
  enabled.value = budget.value.token_limit !== null;
  limit.value =
    budget.value.token_limit === null ? "" : String(budget.value.token_limit);
  draftHash.value = budget.value.configuration_hash;
  dirty.value = false;
}
async function load(resetDraft = false) {
  const key = scopeKey.value;
  if (loading.value && loadingKey === key) return;
  loading.value = true;
  loadingKey = key;
  const current = ++epoch;
  try {
    const value = await budgetsApi.status({ ...props.scope });
    if (current !== epoch) return;
    state.value = value;
    if (!dirty.value || resetDraft) applyDraft();
  } catch (e) {
    if (current === epoch)
      error.value = e instanceof Error ? e.message : "预算读取失败";
  } finally {
    if (current === epoch) loading.value = false;
  }
}
watch(
  scopeKey,
  () => {
    epoch++;
    state.value = null;
    dirty.value = false;
    error.value = message.value = selected.value = reason.value = "";
    void load();
  },
  { immediate: true },
);
onMounted(() => {
  timer = setInterval(() => void load(), 4000);
});
onUnmounted(() => {
  epoch++;
  clearInterval(timer);
});
async function save() {
  const value = enabled.value ? Number(limit.value) : null;
  if (
    enabled.value &&
    (!String(limit.value).trim() || !Number.isSafeInteger(value) || value! < 0)
  ) {
    error.value = "请输入非负整数 Token 上限，0 表示禁止新请求。";
    return;
  }
  const key = scopeKey.value,
    scope = { ...props.scope };
  const originalLimit = String(limit.value),
    originalEnabled = enabled.value;
  saving.value = true;
  error.value = message.value = "";
  try {
    const result = await budgetsApi.configure(scope, value, draftHash.value);
    if (key !== scopeKey.value) return;
    epoch++;
    loading.value = false;
    state.value = result;
    if (
      String(limit.value) === originalLimit &&
      enabled.value === originalEnabled
    )
      applyDraft();
    else {
      draftHash.value = result.budgets[0]!.configuration_hash;
      dirty.value = true;
    }
    message.value =
      value === null
        ? "已关闭这一范围的预算限制。"
        : "预算已保存，下次请求前生效。";
  } catch (e) {
    if (key === scopeKey.value)
      error.value = e instanceof Error ? e.message : "保存失败";
  } finally {
    saving.value = false;
  }
}
function choose(item: Reservation) {
  if (!confirmDiscard(!!reason.value || !!String(actual.value))) return;
  selected.value = item.attempt_id;
  action.value = "keep_unknown";
  actual.value = reason.value = "";
  message.value = error.value = "";
}
async function resolve() {
  if (!target.value || !reason.value.trim()) return;
  const value = Number(actual.value);
  if (
    action.value === "record_known" &&
    (!String(actual.value).trim() || !Number.isSafeInteger(value) || value < 0)
  ) {
    error.value = "补记总量必须是已核对的非负整数。";
    return;
  }
  const key = scopeKey.value,
    attempt = target.value.attempt_id;
  saving.value = true;
  error.value = message.value = "";
  try {
    await budgetsApi.resolve(attempt, {
      action: action.value,
      reason: reason.value.trim(),
      ...(action.value === "record_known" ? { actual_tokens: value } : {}),
    });
    if (key !== scopeKey.value) return;
    selected.value = "";
    reason.value = "";
    actual.value = "";
    message.value = "核对记录已保存；没有精确用量的消费继续保留额度。";
    await load();
  } catch (e) {
    if (key === scopeKey.value)
      error.value = e instanceof Error ? e.message : "核对失败";
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <section class="panel" aria-label="预算控制">
    <h2>共享预算</h2>
    <p class="muted">
      本范围会与其他已配置上限同时检查。输入按序列化字节估算并预留输出上限，供应商返回用量后结算；未知消费继续占用额度。这是请求控制，不保证供应商账单硬上限。
    </p>
    <template v-if="budget">
      <dl class="facts">
        <dt>已结算 Token（含人工核对）</dt>
        <dd>{{ count(budget.actual_tokens) }}</dd>
        <dt>仍占用的预留额度</dt>
        <dd>{{ count(budget.reserved_tokens) }}</dd>
        <dt>剩余额度</dt>
        <dd>
          {{
            budget.token_limit === null
              ? "未启用限制"
              : count(budget.remaining_tokens)
          }}
        </dd>
        <dt>未结算请求</dt>
        <dd>{{ budget.unsettled_attempts }}</dd>
      </dl>
      <p v-if="budget.blocked" class="inline-warning" role="status">
        已达到预算阈值，禁止新模型请求。
      </p>
      <p v-else-if="budget.warning" class="inline-warning" role="status">
        已消耗或预留至少 80% 的额度。
      </p>
      <form class="form-stack" @submit.prevent="save">
        <label
          >预算开关<select
            v-model="enabled"
            aria-label="预算开关"
            @change="dirty = true"
          >
            <option :value="false">不限制这一范围</option>
            <option :value="true">设置 Token 上限</option>
          </select></label
        >
        <label v-if="enabled"
          >Token 上限<input
            v-model="limit"
            inputmode="numeric"
            type="number"
            min="0"
            step="1"
            required
            @input="dirty = true"
        /></label>
        <p class="muted">
          0 会阻止新请求；关闭限制不会删除历史消费。Continue
          仍受项目、当日及交付预算约束。此处日期均为 UTC。
        </p>
        <div class="button-row">
          <button class="primary" :disabled="saving">保存预算</button
          ><button
            type="button"
            :disabled="saving || loading"
            @click="confirmDiscard(dirty) && load(true)"
          >
            重新读取配置
          </button>
        </div>
      </form>
    </template>
    <p v-else class="muted">正在读取预算…</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <details v-if="unsettled.length">
      <summary>核对 {{ unsettled.length }} 个未结算请求</summary>
      <p class="muted">
        正在生成时用量待确认。只有请求已经结束，才可手动核对；保留未知不会释放额度。补记总量不伪造供应商输入／输出细项或费用。
      </p>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>调用身份</th>
              <th>初始预留估算 / 已知用量</th>
              <th>核对记录</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in unsettled" :key="item.attempt_id">
              <td class="mono">
                {{ item.attempt_id }}<small>{{ item.run_id }}</small>
              </td>
              <td>
                {{ count(item.reserved_tokens) }} /
                {{
                  item.actual_tokens === null
                    ? "未确认"
                    : count(item.actual_tokens)
                }}
              </td>
              <td>{{ item.audit.length }}</td>
              <td>
                <button :disabled="saving" @click="choose(item)">核对</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <form v-if="target" class="form-stack" @submit.prevent="resolve">
        <PanelClose label="关闭用量核对" @close="closeResolution" />
        <p class="mono">{{ target.attempt_id }}</p>
        <label
          >核对方式<select v-model="action" :disabled="saving">
            <option value="keep_unknown">仍未知，保留预留额度</option>
            <option value="record_known">已核对，补记实际总 Token</option>
          </select></label
        >
        <label v-if="action === 'record_known'"
          >实际总 Token<input
            v-model="actual"
            :disabled="saving"
            type="number"
            min="0"
            step="1"
            required
        /></label>
        <label
          >核对依据<input
            v-model="reason"
            :disabled="saving"
            required
            maxlength="256"
            placeholder="例如：已核对供应商请求明细"
        /></label>
        <button :disabled="saving || !reason.trim()">保存核对记录</button>
      </form>
    </details>
  </section>
</template>
