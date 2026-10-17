<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { chooseDirectory } from "../api/directories";

const props = withDefaults(
  defineProps<{ modelValue: string; initiallyOpen?: boolean }>(),
  { initiallyOpen: false },
);
const emit = defineEmits<{ "update:modelValue": [value: string]; close: [] }>();
const busy = ref(false);
const manual = ref(false);
const message = ref("");
const input = ref<HTMLInputElement>();
const panel = ref<HTMLElement>();
const launch = ref<HTMLButtonElement>();
let controller: AbortController | undefined;
let disposed = false;
let dismissed = false;
let previousFocus: HTMLElement | null = null;

async function choose() {
  if (busy.value) return;
  dismissed = false;
  manual.value = false;
  busy.value = true;
  message.value = "请在运行 Eventide 服务的电脑上选择文件夹。";
  const current = new AbortController();
  controller = current;
  try {
    const result = await chooseDirectory(current.signal);
    if (disposed || current.signal.aborted) return;
    if (result.status === "selected") {
      emit("update:modelValue", result.path);
      message.value = "目录已填入，请确认后添加项目。";
    } else if (result.status === "cancelled") {
      message.value = "已取消选择。";
      dismissed = true;
      previousFocus?.focus();
      emit("close");
    } else {
      message.value =
        result.status === "busy"
          ? "另一个目录选择窗口仍在使用中，请稍后重试。"
          : result.status === "timed_out"
            ? "选择已超时，请重试或手动输入路径。"
            : "此服务环境无法打开系统窗口，可手动输入服务电脑上的路径。";
    }
  } catch {
    if (!disposed && !current.signal.aborted)
      message.value = "未能打开目录选择窗口，请重试或手动输入路径。";
  } finally {
    if (controller === current) {
      controller = undefined;
      busy.value = false;
      if (!disposed && !dismissed && !manual.value)
        await nextTick(() => launch.value?.focus());
    }
  }
}
function close() {
  dismissed = true;
  controller?.abort();
  manual.value = false;
  message.value = "";
  emit("close");
  previousFocus?.focus();
}
async function showManual() {
  controller?.abort();
  manual.value = true;
  await nextTick();
  input.value?.focus();
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    close();
  }
}
onMounted(() => {
  previousFocus =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  panel.value?.focus();
  if (props.initiallyOpen) void choose();
});
onBeforeUnmount(() => {
  disposed = true;
  controller?.abort();
});
</script>

<template>
  <section
    ref="panel"
    tabindex="-1"
    class="folder-picker"
    aria-label="选择项目目录"
    @keydown="keydown"
  >
    <div class="folder-actions">
      <button ref="launch" type="button" :disabled="busy" @click="choose">
        {{ busy ? "正在选择…" : "选择文件夹" }}
      </button>
      <button type="button" @click="showManual">手动输入路径</button>
      <button
        type="button"
        aria-label="关闭目录选择"
        title="关闭"
        @click="close"
      >
        ×
      </button>
    </div>
    <p v-if="message" role="status">{{ message }}</p>
    <div v-if="manual" class="folder-manual">
      <button
        type="button"
        aria-label="返回目录选择"
        @click="
          manual = false;
          launch?.focus();
        "
      >
        ‹
      </button>
      <label
        >服务电脑上的项目路径
        <input
          ref="input"
          :value="modelValue"
          placeholder="/absolute/project"
          @input="
            emit('update:modelValue', ($event.target as HTMLInputElement).value)
          "
        />
      </label>
    </div>
    <p v-else-if="modelValue" class="folder-path">{{ modelValue }}</p>
  </section>
</template>

<style scoped>
.folder-picker {
  min-width: 0;
}
.folder-actions,
.folder-manual {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.folder-actions > button:last-child {
  margin-left: auto;
  width: 44px;
  min-height: 44px;
  font-size: 1.5rem;
}
.folder-manual label {
  flex: 1;
  min-width: 0;
}
.folder-manual input {
  display: block;
  width: 100%;
  box-sizing: border-box;
}
.folder-path {
  overflow-wrap: anywhere;
}
</style>
