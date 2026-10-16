<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useWorkbench } from "./stores/workbench";
const store = useWorkbench(),
  router = useRouter(),
  path = ref(""),
  adding = ref(false),
  sidebar = ref(false);
onMounted(async () => {
  await store.initialize();
  store.startPolling();
});
onUnmounted(() => store.dispose());
async function selectSession(id: string) {
  try {
    await store.selectSession(id);
    await router.push(`/chat/${encodeURIComponent(id)}`);
    sidebar.value = false;
  } catch (e) {
    store.fail(e);
  }
}
async function createSession() {
  try {
    const id = await store.createSession();
    if (id) await router.push(`/chat/${encodeURIComponent(id)}`);
  } catch (e) {
    store.fail(e);
  }
}
async function add() {
  if (!path.value.trim()) return;
  try {
    await store.addWorkspace(path.value.trim());
    path.value = "";
    adding.value = false;
  } catch (e) {
    store.fail(e);
  }
}
</script>
<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ expanded: sidebar }">
      <a class="brand" href="#/chat"
        ><span class="brand-orbit">◒</span
        ><span>Eventide<small>工程 Agent 工作台</small></span></a
      >
      <div class="workspace-label">
        <label for="workspace">项目</label
        ><button
          class="icon-button"
          @click="adding = !adding"
          aria-label="添加项目"
        >
          ＋
        </button>
      </div>
      <select
        id="workspace"
        :value="store.selectedWorkspace"
        @change="
          store
            .selectWorkspace(($event.target as HTMLSelectElement).value)
            .catch(store.fail)
        "
      >
        <option value="" disabled>选择项目</option>
        <option
          v-for="w in store.workspaces"
          :key="w.workspace_id"
          :value="w.workspace_id"
        >
          {{ w.name }}
        </option>
      </select>
      <form v-if="adding" class="add-workspace" @submit.prevent="add">
        <label for="project-path">本机项目绝对路径</label
        ><input
          id="project-path"
          v-model="path"
          placeholder="/path/to/project"
          required
        /><button class="primary" type="submit">添加项目</button>
      </form>
      <p
        class="workspace-path"
        :title="
          store.workspaces.find(
            (w) => w.workspace_id === store.selectedWorkspace,
          )?.path
        "
      >
        {{
          store.workspaces.find(
            (w) => w.workspace_id === store.selectedWorkspace,
          )?.path || "添加一个本机项目开始"
        }}
      </p>
      <button
        class="new-session"
        :disabled="!store.selectedWorkspace || store.creating"
        @click="createSession"
      >
        ＋ 新建对话
      </button>
      <nav class="session-list" aria-label="项目对话">
        <button
          v-for="s in store.currentSessions"
          :key="s.session_id"
          :class="{ selected: s.session_id === store.selectedSession }"
          @click="selectSession(s.session_id)"
        >
          <span class="status-dot" :class="s.status"></span
          ><span>{{ s.title === "New session" ? "新对话" : s.title }}</span
          ><small v-if="store.active(s.status)">{{
            s.status === "queued" ? "排队中" : "运行中"
          }}</small>
        </button>
        <p v-if="!store.currentSessions.length" class="muted">
          项目对话会显示在这里。
        </p>
      </nav>
      <nav class="product-nav" aria-label="工作台导航">
        <RouterLink to="/chat">◌ 对话</RouterLink
        ><RouterLink to="/queue">≋ 任务队列</RouterLink
        ><RouterLink to="/worktrees">⑂ 工作副本</RouterLink>
        <RouterLink to="/sessions">☷ 会话管理</RouterLink
        ><RouterLink to="/history">↶ 历史运行</RouterLink
        ><RouterLink to="/skills">◇ Skills</RouterLink
        ><RouterLink to="/knowledge">⌕ 知识索引</RouterLink
        ><RouterLink to="/evaluations">✓ 评测</RouterLink
        ><RouterLink to="/deliveries">↗ 修复交付</RouterLink
        ><RouterLink to="/usage">▥ 用量</RouterLink
        ><RouterLink to="/settings">⚙ 模型设置</RouterLink>
      </nav>
      <div class="connection">
        <i :class="{ offline: !store.connected }"></i
        >{{ store.connected ? "本机服务已连接" : "正在重新连接"
        }}<small>运行事实由后端保存</small>
      </div>
    </aside>
    <main class="main-pane">
      <header class="mobile-header">
        <button
          class="icon-button"
          aria-label="切换导航"
          @click="sidebar = !sidebar"
        >
          ☰</button
        ><strong>Eventide</strong>
      </header>
      <div v-if="store.error" role="alert" class="alert error">
        <span>{{ store.error }}</span
        ><button
          class="icon-button"
          aria-label="关闭错误"
          @click="store.error = ''"
        >
          ×
        </button>
      </div>
      <div v-if="store.notice" role="status" class="alert">
        <span>{{ store.notice }}</span
        ><button
          class="icon-button"
          aria-label="关闭提示"
          @click="store.notice = ''"
        >
          ×
        </button>
      </div>
      <div v-if="store.loading" class="page-loading">正在读取工作区与会话…</div>
      <RouterView v-else />
    </main>
  </div>
</template>
