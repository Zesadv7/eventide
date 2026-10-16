<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  deliveriesApi,
  deliveryState,
  observeDeliveries,
  type Delivery,
  type Operation,
  type Patch,
  type Intent,
} from "../api/deliveries";
import { useWorkbench } from "../stores/workbench";
const store = useWorkbench(),
  route = useRoute(),
  router = useRouter();
const project = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.project_id || "",
);
const entries = ref<Delivery[]>([]),
  operations = ref<Operation[]>([]),
  selected = ref("");
const detail = computed(() =>
  entries.value.find((e) => e.delivery_id === selected.value),
);
const state = ref(deliveryState(project.value));
const name = ref(""),
  goal = ref(""),
  checks = ref(""),
  image = ref(""),
  rounds = ref(3),
  tokenLimit = ref(60_000);
const error = ref(""),
  loading = ref(false),
  stopping = ref(""),
  patch = ref<Patch | null>(null),
  reviewed = ref(false),
  more = ref(false);
const locked = computed(() => state.value.active || !!state.value.pending);
const busy = computed(() =>
  operations.value.some(
    (o) =>
      o.accepted.delivery_id === selected.value &&
      ["running", "queued"].includes(o.status),
  ),
);
const canApply = computed(
  () =>
    detail.value?.repair_status === "passed" &&
    !detail.value.applied &&
    !busy.value &&
    patch.value &&
    reviewed.value &&
    patch.value.patch_sha256 === detail.value.verification?.patch_sha256,
);
let revision = 0,
  flight = -1,
  mounted = true,
  timer: ReturnType<typeof setInterval> | undefined;
const seen = new Map<string, string>();
function sync() {
  state.value = deliveryState(project.value);
  const receipt = state.value.receipt;
  if (receipt && seen.get(project.value) !== receipt.run_id) {
    seen.set(project.value, receipt.run_id);
    choose(receipt.delivery_id);
    void refresh();
  }
}
const unsubscribe = observeDeliveries(sync);
function choose(id: string) {
  if (!mounted) return;
  if (selected.value !== id) {
    selected.value = id;
    patch.value = null;
    reviewed.value = false;
  }
  if (route.query.delivery !== id)
    void router.replace({
      query: { ...route.query, delivery: id || undefined },
    });
}
async function refresh(older = false) {
  const owner = project.value,
    version = revision;
  if (!owner || flight === version) return;
  flight = version;
  loading.value = true;
  try {
    const [values, ops] = await Promise.all([
      deliveriesApi.list(owner),
      deliveriesApi.operations(
        owner,
        older ? operations.value.at(-1)?.run_id : undefined,
      ),
    ]);
    if (!mounted || revision !== version) return;
    entries.value = values;
    const ids = new Set(ops.map((o) => o.run_id));
    operations.value = older
      ? [
          ...operations.value,
          ...ops.filter(
            (o) => !operations.value.some((e) => e.run_id === o.run_id),
          ),
        ]
      : [...ops, ...operations.value.filter((o) => !ids.has(o.run_id))];
    if (older || operations.value.length <= 20) more.value = ops.length === 20;
    if (!selected.value && values.length) choose(values.at(-1)!.delivery_id);
    if (
      patch.value &&
      detail.value?.verification?.patch_sha256 !== patch.value.patch_sha256
    ) {
      patch.value = null;
      reviewed.value = false;
    }
  } catch (e) {
    if (mounted && version === revision)
      error.value = e instanceof Error ? e.message : "读取失败";
  } finally {
    if (version === revision) {
      flight = -1;
      loading.value = false;
    }
  }
}
async function submit(intent: Intent) {
  const version = revision;
  error.value = "";
  try {
    await deliveriesApi.submit(project.value, intent);
  } catch (e) {
    if (mounted && version === revision)
      error.value = e instanceof Error ? e.message : "接收失败";
  }
}
function prepare() {
  void submit({ action: "prepare", body: { name: name.value.trim() } });
}
function repair() {
  void submit({
    action: "repair",
    delivery: selected.value,
    body: {
      goal: goal.value,
      checks: checks.value
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
      image: image.value.trim() || null,
      max_rounds: rounds.value,
      ...(tokenLimit.value === 60_000
        ? {}
        : { observed_token_limit: tokenLimit.value }),
    },
  });
}
function apply() {
  if (canApply.value && patch.value)
    void submit({
      action: "apply",
      delivery: selected.value,
      body: { patch_sha256: patch.value.patch_sha256 },
    });
}
async function inspectPatch() {
  const version = revision,
    identity = selected.value;
  reviewed.value = false;
  try {
    const value = await deliveriesApi.patch(identity);
    if (mounted && version === revision && identity === selected.value)
      patch.value = value;
  } catch (e) {
    if (mounted && version === revision && identity === selected.value)
      error.value = e instanceof Error ? e.message : "补丁读取失败";
  }
}
async function stop(id: string) {
  const version = revision;
  stopping.value = id;
  try {
    await deliveriesApi.cancel(id);
    if (mounted && version === revision) void refresh();
  } catch (e) {
    if (mounted && version === revision)
      error.value = e instanceof Error ? e.message : "停止失败";
  } finally {
    if (version === revision) stopping.value = "";
  }
}
function reset() {
  revision++;
  entries.value = [];
  operations.value = [];
  selected.value =
    typeof route.query.delivery === "string" ? route.query.delivery : "";
  patch.value = null;
  reviewed.value = false;
  error.value = "";
  more.value = false;
  stopping.value = "";
  name.value = "";
  goal.value = "";
  checks.value = "";
  image.value = "";
  rounds.value = 3;
  tokenLimit.value = 60_000;
  sync();
  void refresh();
}
watch(project, reset);
watch(
  () => route.query.delivery,
  (value) => {
    const id = typeof value === "string" ? value : "";
    if (id !== selected.value) choose(id);
  },
);
onMounted(() => {
  reset();
  timer = setInterval(() => {
    void refresh();
  }, 2000);
});
onUnmounted(() => {
  mounted = false;
  clearInterval(timer);
  unsubscribe();
});
const status: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  passed: "验收通过",
  stopped: "已停止",
};
</script>
<template>
  <section class="page deliveries">
    <h2>修复交付</h2>
    <p>
      在独立副本修改，固定验收命令，最多三轮验收。源项目只在你明确采纳时更新。
    </p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="state.pending">
      上次接收尚未确认，重试沿用原请求内容与身份。
      <span v-if="state.pending.action === 'repair'">
        原请求 Token 软限：{{
          state.pending.body.observed_token_limit ?? 60_000
        }}。 </span
      ><button :disabled="state.active" @click="submit(state.pending!)">
        重试原交付请求
      </button>
    </p>
    <form @submit.prevent="prepare">
      <label
        >副本名称<input
          v-model="name"
          required
          pattern="[a-z0-9][a-z0-9-]{0,47}"
          maxlength="48"
          :disabled="locked"
          placeholder="fix-login"
      /></label>
      <button :disabled="locked || !project">创建交付副本</button>
    </form>
    <h3>交付记录</h3>
    <p v-if="!entries.length">
      {{ loading ? "读取中…" : "暂无已创建的副本。" }}
    </p>
    <div class="delivery-list">
      <button
        v-for="entry in entries"
        :key="entry.delivery_id"
        :class="{ selected: entry.delivery_id === selected }"
        @click="choose(entry.delivery_id)"
      >
        {{ entry.name }} ·
        {{
          entry.applied
            ? "已采纳"
            : status[entry.repair_status || ""] || "待修复"
        }}
      </button>
    </div>
    <article v-if="detail">
      <h3>{{ detail.name }}</h3>
      <p>{{ detail.delivery_id }}</p>
      <p>执行副本：{{ detail.receipt.checkout }}</p>
      <form v-if="!detail.repair && !detail.applied" @submit.prevent="repair">
        <label
          >修复目标<textarea
            v-model="goal"
            required
            maxlength="20000"
            :disabled="locked || busy"
          />
        </label>
        <label
          >累计 Token 软限<input
            v-model.number="tokenLimit"
            type="number"
            required
            min="1"
            max="1000000"
            step="1"
            :disabled="locked || busy"
        /></label>
        <label
          >验收命令（每行一条，最多四条）<textarea
            v-model="checks"
            required
            :disabled="locked || busy"
            placeholder="python -m pytest -q"
          />
        </label>
        <label
          >容器镜像（留空使用本地执行）<input
            v-model="image"
            :disabled="locked || busy"
        /></label>
        <label
          >最多验收轮数<select
            v-model.number="rounds"
            :disabled="locked || busy"
          >
            <option :value="1">1</option>
            <option :value="2">2</option>
            <option :value="3">3</option>
          </select></label
        >
        <p class="muted">
          验收命令将在所选执行后端运行。本地执行不提供操作系统隔离。整条交付最多
          36 次模型请求、15 分钟；Token
          软限默认为60,000，按供应商已报告输入与输出累计，
          不代表套餐费用或账单上限。使用项目和每日共享预算，接收后不能更改。
        </p>
        <button :disabled="locked || busy">开始有界修复</button>
      </form>
      <template v-if="detail.repair">
        <p>
          修复状态：{{
            status[detail.repair_status || ""] || detail.repair_status
          }}。停止原因：{{ detail.repair_result?.reason || "尚未结束" }}
        </p>
        <p>固定验收：{{ detail.repair.checks }}</p>
        <p>固定 Token 软限：{{ detail.repair.limits.observed_tokens }}</p>
        <div
          v-for="round in detail.rounds"
          :key="String(round.round)"
          class="round"
        >
          <h4>第 {{ round.round }} 轮 · {{ round.status }}</h4>
          <div v-for="(check, index) in round.checks" :key="index">
            <p>
              {{ check.is_error ? "失败" : "完成" }} · 退出码
              {{ check.metadata?.exit_code ?? "未知" }}
            </p>
            <pre>{{ check.content }}</pre>
          </div>
        </div>
        <p v-if="detail.repair_result">
          累计
          {{ detail.repair_result.usage.attempt_count }} 次调用；供应商确认输入
          {{
            detail.repair_result.usage.reported_totals?.input_tokens ?? "未知"
          }}
          Token，输出
          {{
            detail.repair_result.usage.reported_totals?.output_tokens ?? "未知"
          }}
          Token； 未知用量
          {{ detail.repair_result.usage.unknown_attempts }} 次。
          {{
            detail.repair_result.usage.complete
              ? ""
              : "统计不完整，请查看调用明细。"
          }}
        </p>
        <RouterLink
          :to="{
            path: '/usage',
            query: { delivery: detail.delivery_id },
          }"
          >查看累计用量与预算</RouterLink
        >
      </template>
      <p v-if="detail.applied">补丁已采纳到源项目，未自动提交或推送。</p>
      <button v-if="detail.verification" @click="inspectPatch">
        查看验收补丁
      </button>
      <template v-if="patch">
        <pre class="patch">{{ patch.patch }}</pre>
        <p>补丁 SHA-256：{{ patch.patch_sha256 }}</p>
        <label v-if="!detail.applied && detail.repair_status === 'passed'"
          ><input
            v-model="reviewed"
            type="checkbox"
          />我已检查此补丁，确认采纳到源项目</label
        >
        <button
          v-if="!detail.applied"
          :disabled="!canApply || locked"
          @click="apply"
        >
          采纳已检查的补丁
        </button>
      </template>
    </article>
    <h3>后台操作</h3>
    <div
      v-for="operation in operations"
      :key="operation.run_id"
      class="operation"
    >
      <button @click="choose(String(operation.accepted.delivery_id))">
        {{ operation.accepted.action }} · {{ status[operation.status] }} ·
        {{ operation.run_id }}
      </button>
      <p v-if="operation.accepted.action === 'repair'">
        已接收 Token 软限：{{
          operation.accepted.observed_token_limit ?? 60_000
        }}
      </p>
      <p v-if="operation.error" class="inline-error">{{ operation.error }}</p>
      <button
        v-if="['queued', 'running'].includes(operation.status)"
        :disabled="stopping === operation.run_id"
        @click="stop(operation.run_id)"
      >
        {{ stopping === operation.run_id ? "等待清理…" : "停止此操作" }}
      </button>
    </div>
    <button v-if="more" :disabled="loading" @click="refresh(true)">
      加载更早操作
    </button>
  </section>
</template>
<style scoped>
.deliveries {
  max-width: 1000px;
  margin: auto;
  overflow-wrap: anywhere;
}
form,
.delivery-list {
  display: grid;
  gap: 0.8rem;
  margin: 1rem 0;
}
form label {
  display: grid;
  gap: 0.4rem;
}
textarea {
  min-height: 90px;
  width: 100%;
  box-sizing: border-box;
}
input:not([type="checkbox"]),
select {
  min-width: 0;
  max-width: 100%;
}
button {
  white-space: normal;
  overflow-wrap: anywhere;
}
.selected {
  border-color: var(--accent);
}
article,
.operation {
  border-top: 1px solid var(--border);
  padding-top: 1rem;
  margin-top: 1rem;
}
pre {
  max-width: 100%;
  overflow: auto;
  padding: 0.8rem;
  background: var(--bg);
  font-size: 0.8rem;
}
</style>
