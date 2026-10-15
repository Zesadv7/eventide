<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api/client";
import type { Capabilities } from "../api/types";
import { useWorkbench } from "../stores/workbench";
const props = defineProps<{
    area: "skills" | "knowledge" | "evaluations" | "deliveries";
  }>(),
  store = useWorkbench(),
  capabilities = ref<Capabilities | null>(null),
  error = ref("");
let revision = 0;
const labels = {
  skills: "Skills 扩展",
  knowledge: "知识索引",
  evaluations: "评测",
  deliveries: "修复交付",
};
const title = computed(() => labels[props.area]);
watch(
  () => [props.area, store.selectedWorkspace],
  async () => {
    const current = ++revision;
    capabilities.value = null;
    error.value = "";
    if (props.area === "skills" && store.selectedWorkspace)
      try {
        const result = await api.capabilities(store.selectedWorkspace);
        if (current === revision) capabilities.value = result;
      } catch (e) {
        if (current === revision)
          error.value = e instanceof Error ? e.message : "无法读取能力目录";
      }
  },
  { immediate: true },
);
</script>
<template>
  <section class="page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">WORKSPACE / {{ area.toUpperCase() }}</p>
        <h1>{{ title }}</h1>
        <p class="muted">
          {{
            store.workspaces.find(
              (w) => w.workspace_id === store.selectedWorkspace,
            )?.name || "请先选择项目"
          }}
        </p>
      </div>
    </header>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <template v-if="area === 'skills'"
      ><section class="panel">
        <h2>项目可发现的 Skills</h2>
        <p class="muted">
          来自当前后端只读能力目录，尚不代表本轮已加载或已获执行权限。
        </p>
        <div v-if="capabilities?.skills.length" class="skill-grid">
          <article
            v-for="skill in capabilities.skills"
            :key="skill.name"
            class="skill-card"
          >
            <span class="badge">项目 Skill</span>
            <h3>{{ skill.name }}</h3>
            <p>{{ skill.description }}</p>
          </article>
        </div>
        <p v-else class="empty-state compact">
          {{
            store.selectedWorkspace
              ? "当前没有可发现的 Skill。"
              : "先选择项目。"
          }}
        </p>
        <p v-for="note in capabilities?.notes || []" :key="note" class="muted">
          {{ note }}
        </p>
      </section>
      <section class="panel">
        <h2>安装与版本管理</h2>
        <p>
          本地包导入、启停、固定版本与资源管理将在 Skills
          控制面完成后接入。当前页面不会模拟安装成功。
        </p>
      </section></template
    >
    <section v-else class="panel empty-state">
      <div class="empty-symbol">
        {{ area === "knowledge" ? "⌕" : area === "evaluations" ? "✓" : "↗" }}
      </div>
      <h2>{{ title }}控制面尚未接入</h2>
      <p v-if="area === 'knowledge'">
        索引构建、检索试用和增量更新将在真实 API 完成后开放。
      </p>
      <p v-else-if="area === 'evaluations'">
        将展示任务集、后台进度、成功与失败证据，以及完整用量报告。
      </p>
      <p v-else>
        将展示独立工作副本、验收轮次、补丁和人工采纳。当前不会发起写入。
      </p>
      <p class="muted">此处没有模拟数据或假进度，现有能力在迁移验收前保留。</p>
    </section>
  </section>
</template>
