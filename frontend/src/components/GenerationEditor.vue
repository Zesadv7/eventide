<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import {
  configurationStatus,
  generationScope,
  observeConfiguration,
  rejectedConfiguration,
} from "../api/configuration";
import {
  useUnsavedChanges,
  confirmDiscard,
} from "../composables/useUnsavedChanges";
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
  saving = ref(false),
  loading = ref(false),
  refreshRequired = ref(false),
  statusVersion = ref(0);
const modes = ref<Record<string, string>>({}),
  values = ref<Record<string, string>>({});
const draftDirty = ref(false);
useUnsavedChanges(() => scope.value !== "run" && draftDirty.value);
defineExpose({ confirmLeave: () => confirmDiscard(draftDirty.value) });
let revision = 0,
  readRevision = 0,
  mounted = true;
const keys = [
  "temperature",
  "top_p",
  "max_output_tokens",
  "reasoning_effort",
  "thinking",
  "response_format",
] as const;
const scope = computed(() => props.scope || "run");
const mutationSession = computed(() =>
  scope.value === "session" ? props.session : undefined,
);
const identity = computed(() =>
  generationScope(props.model, mutationSession.value),
);
const status = computed(() => {
  void statusVersion.value;
  return configurationStatus(identity.value);
});
const busy = computed(
  () => loading.value || saving.value || status.value.active,
);
let previousScope = identity.value,
  wasActive = status.value.active;
const stopObserving = observeConfiguration(() => {
  const next = configurationStatus(identity.value);
  if (
    previousScope === identity.value &&
    wasActive &&
    !next.active &&
    !saving.value
  )
    refreshRequired.value = true;
  previousScope = identity.value;
  wasActive = next.active;
  statusVersion.value++;
});
onUnmounted(() => {
  mounted = false;
  revision++;
  readRevision++;
  stopObserving();
});
function usePatch(result: GenerationState) {
  draftDirty.value = false;
  const value =
    scope.value === "model"
      ? result.defaults
      : scope.value === "session"
        ? result.session
        : {};
  modes.value = {};
  values.value = {};
  for (const key of keys) {
    modes.value[key] = "inherit";
    if (Object.prototype.hasOwnProperty.call(value, key)) {
      modes.value[key] = value[key] === null ? "default" : "value";
      values.value[key] = String(value[key] ?? "");
    }
  }
  emit("change", patch());
}
async function refresh(initial = false) {
  if (!props.model || status.value.active) return false;
  const version = revision,
    read = ++readRevision;
  loading.value = true;
  try {
    const result = await api.generation(
      props.model,
      scope.value === "model" ? undefined : props.session,
    );
    if (!mounted || version !== revision || read !== readRevision) return false;
    state.value = result;
    if (initial || !status.value.pending) usePatch(result);
    error.value = "";
    refreshRequired.value = false;
    return true;
  } catch (e) {
    if (mounted && version === revision && read === readRevision) {
      error.value = e instanceof Error ? e.message : "无法读取生成配置";
      refreshRequired.value = true;
    }
    return false;
  } finally {
    if (mounted && version === revision && read === readRevision)
      loading.value = false;
  }
}
watch(
  () => [props.model, props.session, scope.value],
  () => {
    revision++;
    readRevision++;
    state.value = null;
    error.value = "";
    saved.value = "";
    modes.value = {};
    values.value = {};
    saving.value = false;
    loading.value = false;
    refreshRequired.value = status.value.active;
    previousScope = identity.value;
    wasActive = status.value.active;
    emit("change", {});
    void refresh(true);
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
        : key === "response_format"
          ? c.response_formats.length > 0
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
  draftDirty.value = true;
  saved.value = "";
  emit("change", patch());
}
async function save() {
  if (
    busy.value ||
    scope.value === "run" ||
    !state.value ||
    !props.model ||
    (scope.value === "session" && !props.session) ||
    (refreshRequired.value && !status.value.pending)
  )
    return;
  const version = revision,
    model = props.model,
    session = mutationSession.value;
  saving.value = true;
  error.value = "";
  saved.value = "";
  try {
    await api.saveGeneration(model, patch(), state.value.config_hash, session);
    if (!mounted || version !== revision) return;
    draftDirty.value = false;
    const fresh = await refresh();
    saved.value = fresh
      ? "保存已确认，已读取当前生成设置；新运行生效。"
      : "保存已确认；当前生成设置读取失败，请刷新后再编辑。";
  } catch (e) {
    if (!mounted || version !== revision) return;
    if (rejectedConfiguration(e)) refreshRequired.value = true;
    error.value = e instanceof Error ? e.message : "保存结果未确认";
  } finally {
    if (mounted && version === revision) saving.value = false;
  }
}
const labels: Record<string, string> = {
  temperature: "温度",
  top_p: "Top P",
  max_output_tokens: "最大输出 Token",
  reasoning_effort: "推理强度",
  thinking: "思考模式",
  response_format: "最终输出格式",
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
    <fieldset :disabled="busy" class="generation-fields">
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
          :step="
            key === 'max_output_tokens'
              ? 1
              : state?.capabilities.sampling_precision != null
                ? 10 ** -state.capabilities.sampling_precision
                : 0.05
          "
          :min="
            key === 'temperature'
              ? 0
              : key === 'top_p'
                ? state?.capabilities.top_p_min || 0.00001
                : 1
          "
          :max="
            key === 'temperature'
              ? (state?.capabilities.temperature_max ?? 2)
              : key === 'top_p'
                ? 1
                : (state?.capabilities.max_output_tokens ?? 1000000)
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
          v-if="modes[key] === 'value' && key === 'response_format'"
          v-model="values[key]"
          aria-label="输出格式值"
          @change="changed"
        >
          <option
            v-for="format in state?.capabilities.response_formats"
            :key="format"
            :value="format"
          >
            {{ format === "json_object" ? "JSON 对象" : "文本" }}
          </option>
        </select>
        <select
          v-if="modes[key] === 'value' && key === 'thinking'"
          v-model="values[key]"
          aria-label="思考模式值"
          @change="changed"
        >
          <option
            v-for="mode in state?.capabilities.thinking_modes ?? [
              'enabled',
              'disabled',
            ]"
            :key="mode"
            :value="mode"
          >
            {{ mode === "enabled" ? "开启" : "关闭" }}
          </option>
        </select>
      </div>
    </fieldset>
    <p v-if="state" class="muted">
      模型默认 {{ JSON.stringify(state.defaults)
      }}<template v-if="props.session">
        · 会话覆盖 {{ JSON.stringify(state.session) }}</template
      >
    </p>
    <p class="muted">
      思考模式与采样参数的组合由后端校验；温度 0
      不保证完全确定性。输出上限不等于任务总预算。JSON
      模式只保证输出格式，不保证事实正确。 JSON
      最终整理会增加一次模型调用，计入原步数和预算。
    </p>
    <p v-if="status.pending && scope !== 'run'" class="alert" role="status">
      上次生成配置请求尚未确认。请恢复原参数后重试；刷新只读取当前值，不清除原请求身份或原配置版本。
    </p>
    <p
      v-if="refreshRequired && scope !== 'run'"
      class="inline-error"
      role="alert"
    >
      请显式刷新生成设置后再编辑，不覆盖其他标签页的更新。
    </p>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="saved" role="status">{{ saved }}</p>
    <button
      v-if="scope !== 'run'"
      :disabled="
        busy || !state || !props.model || (refreshRequired && !status.pending)
      "
      @click="save"
    >
      {{
        saving ? "保存中…" : status.pending ? "重试原生成请求" : "保存生成设置"
      }}
    </button>
    <button
      v-if="scope !== 'run'"
      :disabled="busy"
      @click="confirmDiscard(draftDirty) && refresh()"
    >
      刷新生成设置
    </button>
  </div>
</template>

<style scoped>
.generation-fields {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
</style>
