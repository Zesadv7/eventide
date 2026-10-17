<script setup lang="ts">
import type { Execution } from "../api/types";
defineProps<{ execution?: Execution; runId: string }>();
</script>
<template>
  <details class="run-details sandbox-evidence">
    <summary>本轮沙箱 · 实际执行配置</summary>
    <template v-if="execution?.kind === 'sandbox' && execution.profile">
      <p>
        固定镜像：<span class="mono">{{ execution.profile.image }}</span>
      </p>
      <p>请求镜像：{{ execution.profile.requested_image }}</p>
      <p>
        网络：{{ execution.profile.network }} · 内存
        {{ execution.profile.memory_mb }} MiB · CPU
        {{ execution.profile.cpus }} · 进程 {{ execution.profile.pids_limit }}
      </p>
      <p>
        单次命令 {{ execution.profile.timeout }} 秒 · 输出上限
        {{ execution.profile.output_limit }} 字节
      </p>
      <p>
        候选目录：<span class="mono">{{ execution.working_directory }}</span> ·
        已过滤 {{ execution.skipped ?? 0 }} 项
      </p>
      <a :href="`/api/runs/${encodeURIComponent(runId)}/sandbox-diff`" download
        >导出沙箱补丁</a
      >
      <p class="muted">
        补丁来自本对话共享候选目录的当前状态；请先审阅并自行采纳，不会自动修改源码。
      </p>
    </template>
    <p v-else class="muted">实际沙箱配置尚未确认。</p>
  </details>
</template>

<style scoped>
.sandbox-evidence {
  overflow-wrap: anywhere;
}
</style>
