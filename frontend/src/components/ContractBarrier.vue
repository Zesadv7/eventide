<script setup lang="ts">
import { computed } from "vue";
import { useContractGate } from "../stores/contract";
defineEmits<{ retry: [] }>();
const gate = useContractGate();
const failure = computed(() =>
  gate.result && gate.result.kind !== "compatible" ? gate.result : null,
);
const serverFacts = computed(() => {
  const current = gate.result;
  return current?.kind === "incompatible" && current.server
    ? current.server
    : null;
});
</script>
<template>
  <div
    class="contract-barrier"
    role="alertdialog"
    aria-modal="true"
    aria-labelledby="contract-title"
    aria-describedby="contract-message"
  >
    <section class="contract-card">
      <template v-if="gate.status === 'checking'">
        <h1 id="contract-title">正在检查服务兼容性…</h1>
        <p id="contract-message" class="muted">正在读取本机服务的契约版本。</p>
      </template>
      <template v-else-if="gate.status === 'incompatible'">
        <h1 id="contract-title">前后端版本不兼容</h1>
        <p id="contract-message">{{ failure?.message }}</p>
        <dl v-if="serverFacts" class="contract-facts">
          <div>
            <dt>服务端契约版本</dt>
            <dd>v{{ serverFacts.contract_version }}</dd>
          </div>
          <div>
            <dt>服务端数据版本</dt>
            <dd>v{{ serverFacts.schema_version }}</dd>
          </div>
          <div v-if="serverFacts.runtime_version">
            <dt>服务端程序版本</dt>
            <dd>{{ serverFacts.runtime_version }}</dd>
          </div>
        </dl>
        <p class="muted">
          在完成升级并重新检查之前，工作台的写入操作已被全部阻止。
        </p>
      </template>
      <template v-else>
        <h1 id="contract-title">暂时无法确认服务兼容性</h1>
        <p id="contract-message">{{ failure?.message }}</p>
        <p class="muted">
          这是连接或服务故障，不是版本不匹配；恢复后重新检查即可继续。
        </p>
      </template>
      <button
        v-if="gate.status !== 'checking'"
        class="contract-retry"
        type="button"
        @click="$emit('retry')"
      >
        重新检查
      </button>
    </section>
  </div>
</template>
<style scoped>
.contract-barrier {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
  padding: 24px;
}
.contract-card {
  max-width: 520px;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 32px;
  background: var(--surface);
}
.contract-card h1 {
  font-size: 1.15rem;
  margin: 0 0 12px;
}
.contract-card p {
  margin: 0 0 12px;
  line-height: 1.6;
}
.contract-facts {
  display: flex;
  gap: 24px;
  margin: 0 0 12px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--soft);
}
.contract-facts div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.contract-facts dt {
  font-size: 0.78rem;
  color: var(--muted);
}
.contract-facts dd {
  margin: 0;
  font-weight: 600;
}
.contract-retry {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: var(--bg);
  border-radius: 8px;
  padding: 8px 18px;
  cursor: pointer;
}
.muted {
  color: var(--muted);
}
</style>
