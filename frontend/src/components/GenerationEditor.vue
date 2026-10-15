<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api/client";
import type { Generation, GenerationState } from "../api/types";
const props = defineProps<{
  model: string;
  session?: string;
  scope?: "run" | "session" | "model";
}>();
const emit = defineEmits<{ change: [generation: Generation] }>();
const state = ref<GenerationState | null>(null),
  error = ref(""),
  saved = ref(""),
  saving = ref(false);
const modes = ref<Record<string, string>>({}),
  values = ref<Record<string, string>>({});
let revision = 0;
const keys = [
  "temperature",
  "top_p",
  "max_output_tokens",
  "reasoning_effort",
  "thinking",
] as const;
const scope = computed(() => props.scope || "run");
watch(
  () => [props.model, props.session, scope.value],
  async () => {
    const version = ++revision;
    error.value = "";
    saved.value = "";
    modes.value = {};
    values.value = {};
    emit("change", {});
    if (!props.model) {
      state.value = null;
      return;
    }
    try {
      const result = await api.generation(props.model, props.session);
      if (version !== revision) return;
      state.value = result;
      const patch =
        scope.value === "model"
          ? result.defaults
          : scope.value === "session"
            ? result.session
            : {};
      for (const key of keys) {
        modes.value[key] = "inherit";
        if (Object.prototype.hasOwnProperty.call(patch, key)) {
          const value = patch[key];
          modes.value[key] = value === null ? "default" : "value";
          values.value[key] = String(value ?? "");
        }
      }
    } catch (e) {
      if (version === revision)
        error.value = e instanceof Error ? e.message : "无法加载模型能力";
    }
  },
  { immediate: true },
);
function supported(key: string) {
  const c = state.value?.capabilities;
  if (key === "max_output_tokens") return true;
  if (!c) return false;
  return key === "temperature"
    ? c.temperature_max !== null
    : key === "top_p"
      ? c.top_p
      : key === "reasoning_effort"
        ? c.reasoning_efforts.length > 0
        : c.thinking;
}
function patch(): Generation {
  const result: Record<string, unknown> = {};
  for (const key of keys) {
    const mode = modes.value[key] || "inherit";
    if (mode === "default") result[key] = null;
    else if (mode === "value") {
      const raw = values.value[key] ?? "";
      result[key] = ["temperature", "top_p", "max_output_tokens"].includes(key)
        ? raw === ""
          ? null
          : Number(raw)
        : raw;
    }
  }
  return result as Generation;
}
function changed() {
  saved.value = "";
  emit("change", patch());
}
async function save() {
  saving.value = true;
  error.value = "";
  try {
    state.value = await api.saveGeneration(
      props.model,
      patch(),
      scope.value === "session" ? props.session : undefined,
    );
    saved.value = "设置已保存，新运行生效。";
  } catch (e) {
    error.value = e instanceof Error ? e.message : "保存失败";
  } finally {
    saving.value = false;
  }
}
const labels: Record<string, string> = {
  temperature: "温度",
  top_p: "Top P",
  max_output_tokens: "最大输出 Token",
  reasoning_effort: "推理强度",
  thinking: "思考模式",
};
</script>
<template>
  <div class="generation-editor">
    <p class="muted">
      {{
        scope === "run"
          ? "仅覆盖本次运行"
          : scope === "session"
            ? "此模型在当前会话的默认设置"
            : "此模型的默认设置"
      }}
      ·
      {{
        state?.capabilities.source === "documented"
          ? "文档声明能力"
          : state?.capabilities.source === "declared"
            ? "用户声明能力"
            : "能力尚未确认"
      }}。运行接收后参数固定。
    </p>
    <div v-for="key in keys" :key="key" class="generation-row">
      <label :for="`mode-${scope}-${key}`">{{ labels[key] }}</label
      ><select
        :id="`mode-${scope}-${key}`"
        v-model="modes[key]"
        @change="changed"
      >
        <option value="" disabled>请选择</option>
        <option value="inherit">跟随上层</option>
        <option value="default">服务端默认</option>
        <option value="value" :disabled="!supported(key)">
          自定义{{ !supported(key) ? "（能力未确认）" : "" }}
        </option>
      </select>
      <input
        v-if="
          (modes[key] || 'inherit') === 'value' &&
          ['temperature', 'top_p', 'max_output_tokens'].includes(key)
        "
        v-model="values[key]"
        :aria-label="labels[key]"
        type="number"
        :step="key === 'max_output_tokens' ? 1 : 0.05"
        :min="key === 'temperature' ? 0 : key === 'top_p' ? 0.00001 : 1"
        :max="
          key === 'temperature'
            ? (state?.capabilities.temperature_max ?? 2)
            : key === 'top_p'
              ? 1
              : 1000000
        "
        @input="changed"
      />
      <select
        v-if="modes[key] === 'value' && key === 'reasoning_effort'"
        v-model="values[key]"
        aria-label="推理强度值"
        @change="changed"
      >
        <option
          v-for="effort in state?.capabilities.reasoning_efforts"
          :key="effort"
          :value="effort"
        >
          {{ effort }}
        </option>
      </select>
      <select
        v-if="modes[key] === 'value' && key === 'thinking'"
        v-model="values[key]"
        aria-label="思考模式值"
        @change="changed"
      >
        <option value="enabled">开启</option>
        <option value="disabled">关闭</option>
      </select>
    </div>
    <p v-if="state" class="muted">
      模型默认 {{ JSON.stringify(state.defaults)
      }}<template v-if="props.session">
        · 会话覆盖 {{ JSON.stringify(state.session) }}</template
      >
    </p>
    <p class="muted">
      思考模式与采样参数的组合由后端校验；温度 0
      不保证完全确定性。输出上限不等于任务总预算。
    </p>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="saved" role="status">{{ saved }}</p>
    <button
      v-if="scope !== 'run'"
      :disabled="saving || !props.model"
      @click="save"
    >
      {{ saving ? "保存中…" : "保存生成设置" }}
    </button>
  </div>
</template>
