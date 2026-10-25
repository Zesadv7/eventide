<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { isNavigationFailure, useRoute, useRouter } from "vue-router";
import { confirmUnsavedChanges } from "./composables/useUnsavedChanges";
import { useWorkbench } from "./stores/workbench";
import { useContractGate } from "./stores/contract";
import EventideLogo from "./components/EventideLogo.vue";
import ThemeControl from "./components/ThemeControl.vue";
import ProjectFolderPicker from "./components/ProjectFolderPicker.vue";
import PageBack from "./components/PageBack.vue";
import ContractBarrier from "./components/ContractBarrier.vue";
const store = useWorkbench(),
  gate = useContractGate(),
  router = useRouter(),
  route = useRoute(),
  path = ref(""),
  adding = ref(false),
  sidebar = ref(false);
function savedCollapsed() {
  try {
    return localStorage.getItem("eventide.sidebar.collapsed") === "true";
  } catch {
    return false;
  }
}
const collapsed = ref(savedCollapsed());
const sidebarElement = ref<HTMLElement | null>(null),
  mobileToggle = ref<HTMLButtonElement | null>(null);
const media = window.matchMedia("(max-width: 760px)");
const mobile = ref(media.matches);
function updateViewport() {
  mobile.value = media.matches;
  if (!media.matches) sidebar.value = false;
}
onMounted(() => media.addEventListener("change", updateViewport));
onUnmounted(() => media.removeEventListener("change", updateViewport));
watch(sidebar, async (value) => {
  await nextTick();
  if (value)
    sidebarElement.value?.querySelector<HTMLElement>("a,button")?.focus();
  else if (mobile.value) mobileToggle.value?.focus();
});
function trapDrawer(event: KeyboardEvent) {
  if (!mobile.value || !sidebar.value || event.key !== "Tab") return;
  const elements = Array.from(
    sidebarElement.value?.querySelectorAll<HTMLElement>(
      "a[href],button:not(:disabled),select,input,summary,[tabindex='0']",
    ) || [],
  ).filter((el) => el.getClientRects().length > 0);
  const first = elements[0],
    last = elements.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
watch(collapsed, (value) => {
  try {
    localStorage.setItem("eventide.sidebar.collapsed", String(value));
  } catch {
    /* Optional preference. */
  }
});
watch(
  () => route.fullPath,
  () => {
    sidebar.value = false;
  },
);
const isChat = computed(() => route.path.startsWith("/chat"));
const workspace = computed(() =>
  store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace),
);
const links = [
  ["/projects", "项目管理"],
  ["/queue", "任务队列"],
  ["/imports", "历史导入"],
  ["/worktrees", "工作副本"],
  ["/sessions", "会话管理"],
  ["/history", "历史运行"],
  ["/skills", "Skills"],
  ["/knowledge", "知识索引"],
  ["/memory", "记忆"],
  ["/evaluations", "评测"],
  ["/deliveries", "修复交付"],
  ["/usage", "用量"],
];
// The workbench only boots after the backend contract matches this build.
// An incompatible server keeps the barrier up, so no write path is reachable.
let workbenchBooted = false;
let disposed = false;
async function boot() {
  await gate.runCheck();
  if (disposed || !gate.compatible || workbenchBooted) return;
  workbenchBooted = true;
  await store.initialize();
  if (disposed) {
    store.dispose();
    return;
  }
  store.startPolling();
}
onMounted(() => {
  void boot();
});
onUnmounted(() => {
  disposed = true;
  store.dispose();
});
async function selectSession(id: string) {
  try {
    const result = await router.push(`/chat/${encodeURIComponent(id)}`);
    if (isNavigationFailure(result)) return;
    sidebar.value = false;
  } catch (e) {
    store.fail(e);
  }
}
async function createSession() {
  try {
    if (!route.path.startsWith("/chat")) {
      const result = await router.push("/chat");
      if (isNavigationFailure(result)) return;
    }
    const id = await store.createSession();
    if (id) await router.push(`/chat/${encodeURIComponent(id)}`);
  } catch (e) {
    store.fail(e);
  }
}
async function syncChatRoute() {
  if (route.path.startsWith("/chat")) {
    await router.replace(
      store.selectedSession
        ? `/chat/${encodeURIComponent(store.selectedSession)}`
        : "/chat",
    );
  }
}
async function selectWorkspace(event: Event) {
  const input = event.target as HTMLSelectElement;
  if (!confirmUnsavedChanges(router)) {
    input.value = store.selectedWorkspace;
    return;
  }
  try {
    await store.selectWorkspace(input.value);
    await syncChatRoute();
  } catch (e) {
    store.fail(e);
    input.value = store.selectedWorkspace;
  }
}
async function add() {
  if (!path.value.trim()) return;
  try {
    if (!confirmUnsavedChanges(router)) return;
    await store.addWorkspace(path.value);
    await syncChatRoute();
    path.value = "";
    adding.value = false;
  } catch (e) {
    store.fail(e);
  }
}
</script>
<template>
  <ContractBarrier v-if="gate.blocking" @retry="boot" />
  <div
    v-else
    class="app-shell"
    :class="{ 'sidebar-collapsed': collapsed, 'chat-shell': isChat }"
    @keydown.esc="sidebar = false"
  >
    <button
      v-if="sidebar"
      class="sidebar-scrim"
      tabindex="-1"
      aria-label="关闭导航"
      @click="sidebar = false"
    ></button>
    <aside
      ref="sidebarElement"
      :inert="mobile ? !sidebar : collapsed"
      :aria-hidden="(mobile ? !sidebar : collapsed) || undefined"
      :role="mobile && sidebar ? 'dialog' : undefined"
      :aria-modal="mobile && sidebar ? true : undefined"
      @keydown="trapDrawer"
      class="sidebar"
      :class="{ expanded: sidebar }"
      aria-label="项目与对话"
    >
      <div class="sidebar-brand-row">
        <RouterLink class="brand" to="/chat"
          ><EventideLogo /><span>Eventide</span></RouterLink
        ><button
          class="icon-button collapse-sidebar"
          aria-label="收起侧栏"
          @click="
            collapsed = true;
            sidebar = false;
          "
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="4" y="5" width="16" height="14" rx="3" />
            <path d="M10 5v14" />
          </svg>
        </button>
      </div>
      <button
        class="new-session"
        :disabled="!store.selectedWorkspace || store.creating"
        @click="createSession"
      >
        <span aria-hidden="true">＋</span> 新建对话 <small>New chat</small>
      </button>
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
        @change="selectWorkspace($event)"
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
      <p class="workspace-path" :title="workspace?.path">
        {{ workspace?.path || "添加一个本机项目开始" }}
      </p>
      <form v-if="adding" class="add-workspace" @submit.prevent="add">
        <ProjectFolderPicker
          v-model="path"
          :initially-open="true"
          @close="adding = false"
        /><button class="primary" type="submit" :disabled="!path.trim()">
          添加项目
        </button>
      </form>
      <div class="sidebar-section-label">对话</div>
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
        <p v-if="!store.currentSessions.length" class="muted sidebar-empty">
          每一次探索，从对话开始。
        </p>
      </nav>
      <details class="workspace-tools">
        <summary>工作台工具</summary>
        <nav class="product-nav" aria-label="工作台导航">
          <RouterLink v-for="link in links" :key="link[0]" :to="link[0]!">{{
            link[1]
          }}</RouterLink>
        </nav>
      </details>
      <div class="sidebar-footer">
        <RouterLink class="settings-link" to="/settings"
          ><svg viewBox="0 0 24 24" aria-hidden="true">
            <path
              d="m9 4 1-2h4l1 2 2 1 2-.2 2 3-1 2v3l1 2-2 3-2-.2-2 1-1 2h-4l-1-2-2-1-2 .2-2-3 1-2v-3l-1-2 2-3 2 .2z"
            />
            <circle cx="12" cy="11.5" r="3" /></svg
          >设置</RouterLink
        ><ThemeControl />
        <div class="connection">
          <i :class="{ offline: !store.connected }"></i
          >{{ store.connected ? "本机服务已连接" : "正在重新连接" }}
        </div>
      </div>
    </aside>
    <main class="main-pane" :inert="mobile && sidebar">
      <header class="shell-header">
        <button
          ref="mobileToggle"
          class="icon-button mobile-toggle"
          aria-label="打开导航"
          @click="sidebar = !sidebar"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 7h16M4 12h16M4 17h16" />
          </svg></button
        ><button
          v-if="collapsed"
          class="icon-button expand-sidebar"
          aria-label="展开侧栏"
          @click="collapsed = false"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="4" y="5" width="16" height="14" rx="3" />
            <path d="M10 5v14" />
          </svg></button
        ><RouterLink class="shell-wordmark" to="/chat"
          >Eventide <span>工作台</span></RouterLink
        ><span class="shell-project">{{ workspace?.name }}</span>
      </header>
      <PageBack v-show="!isChat" />
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
