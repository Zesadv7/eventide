<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { useWorkbench } from "../stores/workbench";
import { ApiError } from "../api/client";
import {
  memoryApi,
  memoryPending,
  canRetryMemory,
  type MemoryRecord,
  type MemoryScope,
  type MemoryState,
  type MemorySuggestion,
} from "../api/memory";
import {
  useUnsavedChanges,
  confirmDiscard,
} from "../composables/useUnsavedChanges";
import MemorySource from "../components/MemorySource.vue";
import PanelClose from "../components/PanelClose.vue";
const store = useWorkbench();
const recoveryKey = ref(""),
  recoveryContent = ref("");
const project = computed(
  () =>
    store.workspaces.find((w) => w.workspace_id === store.selectedWorkspace)
      ?.project_id || "",
);
const scope = ref<MemoryScope>("global"),
  search = ref(""),
  showRevoked = ref(false);
const data = ref<MemoryState | null>(null),
  loading = ref(false),
  busy = ref(false),
  error = ref(""),
  notice = ref("");
const pending = ref(memoryPending()),
  retryable = ref(canRetryMemory());
const editing = ref<MemoryRecord | null>(null),
  composing = ref(false),
  key = ref(""),
  content = ref(""),
  baseline = ref("");
const draftScope = ref<MemoryScope>("global"),
  draftProject = ref<string | null>(null);
const draftValue = () => JSON.stringify([key.value, content.value]);
const dirty = computed(
  () => composing.value && draftValue() !== baseline.value,
);
useUnsavedChanges(
  () =>
    dirty.value || busy.value || !!recoveryContent.value || !!recoveryKey.value,
);
const records = computed(() =>
  (data.value?.memories || []).filter(
    (r) =>
      r.scope === scope.value &&
      (showRevoked.value || r.status !== "revoked") &&
      matches(r),
  ),
);
const candidates = computed(() =>
  (data.value?.suggestions || []).filter(
    (r) => r.scope === scope.value && r.status === "pending" && matches(r),
  ),
);
function matches(r: { key: string; content: string }) {
  const value = search.value.trim().toLocaleLowerCase();
  return !value || `${r.key} ${r.content}`.toLocaleLowerCase().includes(value);
}
const valid = computed(
  () =>
    /^[a-z0-9_.-]{1,64}$/.test(key.value) &&
    content.value.trim().length > 0 &&
    content.value.trim().length <= 500 &&
    !/[\r\n]/.test(content.value),
);
const labels = { active: "已启用", paused: "已暂停", revoked: "已撤销" };
let version = 0,
  alive = true;
async function load() {
  const current = ++version,
    owner = project.value;
  loading.value = true;
  error.value = "";
  try {
    const value = await memoryApi.list(owner || undefined);
    if (alive && current === version) data.value = value;
  } catch (e) {
    if (alive && current === version) {
      data.value = null;
      error.value = e instanceof Error ? e.message : "记忆读取失败";
    }
  } finally {
    if (current === version) loading.value = false;
  }
}
function start(record?: MemoryRecord) {
  if (busy.value || !confirmDiscard(dirty.value)) return;
  editing.value = record ? { ...record } : null;
  draftScope.value = record?.scope || scope.value;
  draftProject.value =
    record?.project_id || (scope.value === "project" ? project.value : null);
  key.value = record?.key || "";
  content.value = record?.content || "";
  baseline.value = draftValue();
  composing.value = true;
  notice.value = "";
}
function close() {
  if (!busy.value && confirmDiscard(dirty.value)) {
    composing.value = false;
    editing.value = null;
  }
}
async function act(
  action: () => Promise<unknown>,
  message: string,
  closeEditor = false,
) {
  if (busy.value) return;
  const owner = project.value;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await action();
    if (!alive || owner !== project.value) return;
    if (closeEditor) {
      composing.value = false;
      editing.value = null;
    }
    notice.value = message;
    await load();
  } catch (e) {
    if (!alive || owner !== project.value) return;
    error.value = e instanceof Error ? e.message : "记忆操作未完成";
    if (
      e instanceof ApiError &&
      e.status === 409 &&
      e.mutationOutcome === "rejected"
    ) {
      const conflict =
        "记录已变化或主题冲突。已刷新当前记录；你的草稿仍保留，请核对后重新打开编辑，不会覆盖较新版本。";
      await load();
      error.value = conflict;
    }
  } finally {
    busy.value = false;
    pending.value = memoryPending();
    retryable.value = canRetryMemory();
  }
}
function recover() {
  void act(
    () =>
      memoryApi.recover(recoveryContent.value.trim(), recoveryKey.value.trim()),
    "原请求结果已确认，请核对当前记录。",
  ).then(() => {
    if (!pending.value) {
      recoveryContent.value = "";
      recoveryKey.value = "";
    }
  });
}
function save() {
  if (!valid.value) return;
  const record = editing.value;
  void act(
    () =>
      record
        ? memoryApi.update(record.id, record.revision, {
            content: content.value.trim(),
          })
        : memoryApi.create({
            scope: draftScope.value,
            project_id:
              draftScope.value === "project" ? draftProject.value : null,
            key: key.value,
            content: content.value.trim(),
          }),
    "已保存。新增或普通编辑将在下次 Run 生效。",
    true,
  );
}
function revoke(record: MemoryRecord) {
  if (
    !window.confirm(
      `撤销“${record.key}”？后续请求将停止加入此偏好，但不会擦除历史事件、聊天、摘要、导出或备份，已发送的请求无法撤回。`,
    )
  )
    return;
  void act(
    () => memoryApi.revoke(record.id, record.revision),
    "已撤销，将从下一次请求起停止加入上下文。历史记录仍保留。",
  );
}
function resolve(record: MemorySuggestion, accept: boolean) {
  void act(
    () =>
      memoryApi.resolve(
        record.id,
        record.revision,
        accept ? "accept" : "dismiss",
      ),
    accept ? "候选已确认保存，下次 Run 生效。" : "已忽略此候选。",
  );
}
function toggleRecord(record: MemoryRecord) {
  void act(
    () =>
      memoryApi.update(record.id, record.revision, {
        status: record.status === "active" ? "paused" : "active",
      }),
    record.status === "active"
      ? "已暂停，下次请求起停止加入。"
      : "已恢复，将在下次 Run 中使用。",
  );
}
function toggle() {
  const settings = data.value?.settings;
  if (!settings) return;
  if (
    settings.enabled &&
    !window.confirm(
      "关闭当前安装的偏好记忆？下一次请求起停止加入偏好，历史记录仍保留。",
    )
  )
    return;
  void act(
    () => memoryApi.settings(!settings.enabled, settings.revision),
    settings.enabled
      ? "偏好记忆已关闭，下一次请求起停止加入。"
      : "偏好记忆已开启；新保存的偏好在下次 Run 生效。",
  );
}
function changeScope(event: Event) {
  const target = event.target as HTMLSelectElement;
  if (!confirmDiscard(dirty.value)) {
    target.value = scope.value;
    return;
  }
  composing.value = false;
  editing.value = null;
  scope.value = target.value as MemoryScope;
}
watch(
  project,
  () => {
    version++;
    data.value = null;
    composing.value = false;
    editing.value = null;
    notice.value = "";
    if (!project.value) scope.value = "global";
    void load();
  },
  { immediate: true },
);
onUnmounted(() => {
  alive = false;
  version++;
});
</script>
<template>
  <section class="page memory-page">
    <header class="memory-header">
      <div>
        <h2>偏好记忆</h2>
        <p>保存经过确认的回答与工作偏好，在新运行中使用。</p>
      </div>
      <button :disabled="loading || busy" @click="load">
        {{ loading ? "读取中…" : "刷新" }}
      </button>
    </header>
    <div class="memory-notice">
      <p>
        全局仅指当前 Eventide
        安装；项目偏好只用于对应项目，同主题优先于全局。当前明确请求优先于已保存偏好。
      </p>
      <p>
        偏好不能授予工具权限、改变审批或自动采纳补丁。普通新增、编辑及候选确认下次
        Run 生效；暂停、撤销和总关闭从下一次请求起停止加入。
      </p>
    </div>
    <div v-if="data" class="memory-switch">
      <div>
        <strong
          >当前安装：{{
            data.settings.enabled ? "偏好记忆已开启" : "偏好记忆已关闭"
          }}</strong
        >
        <p v-if="!data.settings.enabled">
          仍可管理已保存内容，关闭期间不加入请求上下文。
        </p>
      </div>
      <button :disabled="busy" @click="toggle">
        {{ data.settings.enabled ? "关闭偏好记忆" : "开启偏好记忆" }}
      </button>
    </div>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <div v-if="pending" class="memory-warning" role="status">
      <p>
        上次操作结果尚未确认。请重试原操作，不要重复创建或另发修改。浏览器刷新后不会保留偏好正文，需重新填写相同内容及版本。
      </p>
      <button
        v-if="retryable"
        :disabled="busy"
        @click="
          act(memoryApi.retry, '原请求结果已确认，请核对刷新后的记录。', true)
        "
      >
        重试原记忆操作
      </button>
      <form v-else @submit.prevent="recover">
        <p>
          待确认目标：{{ pending.method }}
          {{ pending.path }}；原版本与操作已保留。
        </p>
        <label v-if="pending.needsKey"
          >原主题键<input v-model="recoveryKey" maxlength="64" required
        /></label>
        <label v-if="pending.needsContent"
          >原偏好正文<textarea
            v-model="recoveryContent"
            maxlength="500"
            required
          />
        </label>
        <button :disabled="busy" type="submit">核对并重试原操作</button>
      </form>
    </div>
    <div class="memory-toolbar">
      <label
        >范围<select :value="scope" :disabled="busy" @change="changeScope">
          <option value="global">全局（当前安装）</option>
          <option value="project" :disabled="!project">当前项目</option>
        </select></label
      ><label class="search"
        >搜索已加载的主题或内容<input
          v-model="search"
          type="search"
          placeholder="关键词" /></label
      ><label class="check"
        ><input v-model="showRevoked" type="checkbox" />显示已撤销</label
      ><button
        :disabled="busy || !data || (scope === 'project' && !project)"
        @click="start()"
      >
        新增偏好
      </button>
    </div>
    <form v-if="composing" class="memory-editor" @submit.prevent="save">
      <PanelClose label="关闭偏好编辑" :disabled="busy" @close="close" />
      <h3>
        {{ editing ? "编辑偏好" : "新增偏好" }} ·
        {{ draftScope === "global" ? "全局（当前安装）" : "当前项目" }}
      </h3>
      <p v-if="editing">
        正在编辑版本 {{ editing.revision }}，保存时会检查版本。
      </p>
      <label
        >主题键<input
          v-model="key"
          :disabled="busy || !!editing"
          maxlength="64"
          pattern="[a-z0-9_.-]+"
          placeholder="response_language"
          required /></label
      ><small
        >1–64 个小写字母、数字、点、下划线或连字符；同范围内不可重复。</small
      ><label
        >偏好内容<textarea
          v-model="content"
          :disabled="busy"
          maxlength="500"
          rows="3"
          placeholder="例如：用中文回答"
          required
        /></label
      ><small
        >{{ content.length }}/500 ·
        单行内容。请勿保存密码、密钥或执行权限指令。</small
      >
      <div class="actions">
        <button type="submit" :disabled="busy || !valid">
          {{ busy ? "正在保存…" : "保存偏好" }}</button
        ><button type="button" :disabled="busy" @click="close">取消</button>
      </div>
    </form>
    <section v-if="candidates.length" aria-label="待确认候选">
      <h3>待确认候选（{{ candidates.length }}）</h3>
      <p>候选尚未启用。核对内容、范围和来源后再确认。</p>
      <article
        v-for="candidate in candidates"
        :key="candidate.id"
        class="memory-card candidate"
      >
        <h4>{{ candidate.key }}</h4>
        <p class="content">{{ candidate.content }}</p>
        <MemorySource :source="candidate.source" />
        <div class="actions">
          <button :disabled="busy" @click="resolve(candidate, true)">
            确认保存</button
          ><button :disabled="busy" @click="resolve(candidate, false)">
            忽略候选
          </button>
        </div>
      </article>
    </section>
    <section aria-label="已保存偏好">
      <h3>已保存偏好（{{ records.length }}）</h3>
      <p v-if="!records.length && !loading">
        {{ search ? "没有匹配的偏好。" : "此范围暂无偏好。" }}
      </p>
      <article v-for="record in records" :key="record.id" class="memory-card">
        <div class="card-title">
          <h4>{{ record.key }}</h4>
          <span>{{ labels[record.status] }} · 版本 {{ record.revision }}</span>
        </div>
        <p class="content">{{ record.content }}</p>
        <MemorySource :source="record.source" />
        <div v-if="record.status !== 'revoked'" class="actions">
          <button :disabled="busy" @click="start(record)">编辑</button
          ><button :disabled="busy" @click="toggleRecord(record)">
            {{ record.status === "active" ? "暂停" : "恢复" }}</button
          ><button :disabled="busy" @click="revoke(record)">撤销</button>
        </div>
        <p v-else class="muted">已撤销的记录不能恢复，历史证据仍保留。</p>
      </article>
    </section>
    <p class="memory-warning">
      {{
        data?.warning ||
        "撤销只停止后续请求加入，不会擦除历史事件、原聊天、摘要、导出或备份；已发送的请求无法撤回。"
      }}
    </p>
    <details class="memory-help">
      <summary>如何从对话保存偏好</summary>
      <p>使用明确单行格式：记住：项目：response_language=用中文回答</p>
      <p>
        将“项目”改成“全局”可用于当前安装。普通偏好表达不会自动获得执行权限；重复的结构化偏好只形成待确认候选。
      </p>
    </details>
  </section>
</template>
<style scoped>
.memory-page {
  max-width: 1080px;
  margin: auto;
  overflow-wrap: anywhere;
}
.memory-header,
.memory-switch,
.card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.memory-header h2,
h4 {
  margin: 0;
}
.memory-header p,
.memory-switch p {
  margin: 0.5rem 0;
}
.memory-notice,
.memory-warning {
  padding: 0.85rem 1rem;
  background: var(--soft);
  border-radius: 10px;
  font-size: 0.9rem;
}
.memory-notice p {
  margin: 0.35rem 0;
}
.memory-switch {
  margin: 1rem 0;
  padding: 1rem;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.memory-toolbar {
  display: flex;
  gap: 0.8rem;
  align-items: end;
  flex-wrap: wrap;
  margin: 1.5rem 0;
}
label {
  display: grid;
  gap: 0.4rem;
  font-size: 0.9rem;
}
.search {
  flex: 1;
  min-width: 180px;
}
.check {
  display: flex;
  align-items: center;
  align-self: center;
}
.memory-editor {
  display: grid;
  gap: 0.7rem;
  padding: 1rem;
  border: 1px solid var(--accent);
  border-radius: 12px;
  margin: 1rem 0;
}
.memory-editor h3 {
  margin: 0;
}
textarea {
  resize: vertical;
}
small {
  color: var(--muted);
}
.memory-card {
  padding: 1rem;
  margin: 0.8rem 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
}
.card-title span {
  font-size: 0.85rem;
  color: var(--muted);
}
.content {
  white-space: pre-wrap;
  line-height: 1.6;
}
.actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-top: 0.8rem;
}
.memory-help {
  margin-top: 1rem;
}
summary {
  cursor: pointer;
}
@media (max-width: 480px) {
  .memory-toolbar > label {
    width: 100%;
  }
  .memory-switch {
    align-items: flex-start;
  }
}
</style>
