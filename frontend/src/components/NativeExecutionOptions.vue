<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { request } from "../api/client";
import type { components } from "../api/schema";
const props = defineProps<{
  profile: "read-only" | "workspace-write";
  backend: string;
  frozen: boolean;
}>();
const emit = defineEmits<{
  "update:profile": [value: "read-only" | "workspace-write"];
  "update:backend": [value: string];
  ready: [value: boolean];
}>();
const status = ref<components["schemas"]["NativeCapabilitiesResponse"] | null>(
  null,
);
const error = ref("");
let revision = 0;
async function load() {
  const current = ++revision;
  emit("ready", false);
  error.value = "";
  try {
    const result = await request<
      components["schemas"]["NativeCapabilitiesResponse"]
    >("/api/runtime/native-capabilities");
    if (current !== revision) return;
    status.value = result;
    const selected = props.backend || result.default_backend;
    emit(
      "ready",
      result.backends.some((item) => item.kind === selected && item.available),
    );
  } catch (e) {
    if (current === revision) error.value = String(e);
  }
}
onMounted(load);
onBeforeUnmount(() => {
  revision++;
});
</script>

<template>
  <div class="sandbox-options">
    <strong>原生隔离 · 独立工作副本</strong>
    <p class="muted">
      默认关闭。当前只支持文件读取、查找路径、写入和精确编辑；Shell、Skills、MCP、检索、旁枝和工具网络暂未开放。模型请求仍由本机连接服务商。修改留在候选副本，采纳需要你确认。
    </p>
    <label
      >文件权限
      <select
        :value="profile"
        :disabled="frozen"
        aria-label="原生文件权限"
        @change="
          emit(
            'update:profile',
            ($event.target as HTMLSelectElement).value as
              'read-only' | 'workspace-write',
          )
        "
      >
        <option value="read-only">只读</option>
        <option value="workspace-write">允许修改工作副本</option>
      </select>
    </label>
    <label
      >平台后端
      <select
        :value="backend"
        :disabled="frozen"
        aria-label="原生平台后端"
        @change="
          emit('update:backend', ($event.target as HTMLSelectElement).value);
          load();
        "
      >
        <option value="">使用本机可用后端</option>
        <option
          v-for="item in status?.backends || []"
          :key="item.kind"
          :value="item.kind"
          :disabled="!item.available"
        >
          {{ item.kind }} · {{ item.available ? "已找到" : "不可用" }}
        </option>
      </select>
    </label>
    <p v-if="!status && !error" role="status">正在检查原生执行环境…</p>
    <p v-for="item in status?.backends || []" :key="item.kind" class="muted">
      {{ item.kind }}：{{ item.detail }}
    </p>
    <p v-if="status" class="muted">{{ status.verification_detail }}</p>
    <p v-if="error" class="inline-error" role="alert">
      环境检查失败：{{ error }}
    </p>
    <button type="button" @click="load">重新检查环境</button>
    <p v-if="frozen" class="muted">
      本对话已固定权限与后端；调整环境请新建对话。
    </p>
  </div>
</template>
