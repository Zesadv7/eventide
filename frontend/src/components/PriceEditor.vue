<script setup lang="ts">
import { onMounted, ref } from "vue";
import { budgetsApi, type Price } from "../api/budgets";
import { useWorkbench } from "../stores/workbench";

const store = useWorkbench();
const prices = ref<Price[]>([]),
  provider = ref(store.provider?.provider || "openai_responses"),
  model = ref(store.provider?.model || "");
const category = ref<Price["category"]>("input_tokens"),
  currency = ref("CNY"),
  rate = ref("");
const effective = ref(new Date().toISOString().slice(0, 19)),
  saving = ref(false),
  error = ref(""),
  message = ref("");
const labels: Record<Price["category"], string> = {
  input_tokens: "输入（扣除已单独计价的缓存）",
  output_tokens: "输出（扣除已单独计价的推理）",
  cache_read_input_tokens: "缓存读取",
  cache_creation_input_tokens: "缓存写入",
  reasoning_tokens: "推理 Token",
};
async function load() {
  try {
    prices.value = (await budgetsApi.prices()).prices;
  } catch (e) {
    error.value = e instanceof Error ? e.message : "价格读取失败";
  }
}
onMounted(load);
async function save() {
  saving.value = true;
  error.value = message.value = "";
  try {
    const body = {
      provider: provider.value,
      model: model.value.trim(),
      category: category.value,
      currency: currency.value.trim().toUpperCase(),
      price_per_million: rate.value.trim(),
      effective_at: new Date(`${effective.value}Z`).toISOString(),
    };
    // Equal reviewed pricing content retains the same immutable version identity,
    // including when a lost response is retried after a browser refresh.
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(JSON.stringify(body)),
    );
    const version =
      "price_" +
      Array.from(new Uint8Array(digest), (b) =>
        b.toString(16).padStart(2, "0"),
      ).join("");
    await budgetsApi.registerPrice({ ...body, version_id: version });
    message.value =
      "价格版本已保存。历史费用快照保留原版本，新价格用于后续请求。";
    await load();
  } catch (e) {
    error.value = e instanceof Error ? e.message : "价格保存失败";
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <section class="panel">
    <details>
      <summary>管理价格版本（{{ prices.length }} 条）</summary>
      <p class="muted">
        请按供应商正式价目填写每百万 Token
        的价格。无价格显示未知；不同币种分开统计，不自动换汇。缓存与推理属于总量的细项，不能再次加进总
        Token。
      </p>
      <form class="form-stack" @submit.prevent="save">
        <label
          >价格对应的 API 类型<select v-model="provider">
            <option value="openai_responses">OpenAI Responses</option>
            <option value="openai_compatible">Chat Completions 兼容接口</option>
            <option value="anthropic">Anthropic Messages</option>
          </select></label
        >
        <label
          >价格对应的模型<input v-model="model" required maxlength="256"
        /></label>
        <label
          >计费类别<select v-model="category" aria-label="计费类别">
            <option v-for="(label, key) in labels" :key="key" :value="key">
              {{ label }}
            </option>
          </select></label
        >
        <label
          >币种<input
            v-model="currency"
            required
            pattern="[A-Za-z]{3}"
            maxlength="3"
        /></label>
        <label
          >每百万 Token 单价<input
            v-model="rate"
            required
            inputmode="decimal"
            pattern="[0-9]+(\.[0-9]+)?"
            placeholder="填写准确小数"
        /></label>
        <label
          >生效时间（UTC）<input
            v-model="effective"
            type="datetime-local"
            step="1"
            required
        /></label>
        <button class="primary" :disabled="saving">保存价格版本</button>
      </form>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <p v-if="message" role="status">{{ message }}</p>
      <div v-if="prices.length" class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>模型 / 接入</th>
              <th>类别</th>
              <th>币种 / 单价</th>
              <th>生效时间 / 固定版本</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="price in prices" :key="price.version_id">
              <td>
                {{ price.model }}<small>{{ price.provider }}</small>
              </td>
              <td>{{ labels[price.category] }}</td>
              <td>{{ price.currency }} {{ price.price_per_million }}</td>
              <td>
                {{ price.effective_at
                }}<small class="mono">{{ price.version_id }}</small>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
  </section>
</template>
