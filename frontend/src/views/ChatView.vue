<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useWorkbench } from "../stores/workbench";
import { api } from "../api/client";
import type { Generation, Message } from "../api/types";
import GenerationEditor from "../components/GenerationEditor.vue";
import SafeContent from "../components/SafeContent.vue";
const store = useWorkbench(),
  route = useRoute(),
  mode = ref("auto"),
  model = ref(""),
  generation = ref<Generation>({}),
  modelList = ref<string[]>([]),
  uploading = ref(false);
const draft = computed({
  get: () => store.drafts[store.selectedSession] || "",
  set: (text) => store.setDraft(store.selectedSession, text),
});
const session = computed(() => store.currentSession),
  run = computed(() => store.currentRun),
  isActive = computed(() => store.active(run.value?.status));
const busyOther = computed(() => store.workspaceBusy && !isActive.value),
  unknown = computed(() => run.value?.reason === "unknown_tool_outcome");
const preview = computed(() =>
  run.value ? store.previews[run.value.run_id] : null,
);
const pending = computed(() => store.pending[store.selectedSession]);
watch(
  () => [route.params.sessionId, store.loading],
  async ([identity, loading]) => {
    if (
      !loading &&
      typeof identity === "string" &&
      identity !== store.selectedSession
    )
      try {
        await store.selectSession(identity);
      } catch (e) {
        store.fail(e);
      }
  },
  { immediate: true },
);
watch(
  () => [store.selectedSession, store.provider?.model],
  () => {
    model.value = store.provider?.model || "";
    generation.value = {};
  },
  { immediate: true },
);
async function refreshModels() {
  try {
    const result = await api.models();
    modelList.value = result.models;
    if (result.error) store.error = result.error;
  } catch (e) {
    store.fail(e);
  }
}
const models = computed(() => [
  ...new Set(
    [store.provider?.model, model.value, ...modelList.value].filter(
      Boolean,
    ) as string[],
  ),
]);
function content(message: Message) {
  if (typeof message.content === "string") return message.content;
  return message.content
    .filter((b) => b.type === "text" && typeof b.text === "string")
    .map((b) => b.text)
    .join("\n");
}
async function submit() {
  if (!draft.value.trim()) return;
  try {
    await store.submit({
      prompt: draft.value,
      mode: mode.value,
      model: model.value || null,
      generation: generation.value,
      attachment_ids: (store.attachments[store.selectedSession] || []).map(
        (a) => a.attachment_id,
      ),
    });
  } catch (e) {
    store.fail(e);
  }
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  const sid = store.selectedSession;
  if (file.size > 10 * 1024 * 1024) {
    store.error = "单个附件不能超过 10MB";
    return;
  }
  uploading.value = true;
  try {
    const data = await file.arrayBuffer();
    let binary = "";
    for (const byte of new Uint8Array(data))
      binary += String.fromCharCode(byte);
    await api.uploadAttachment(
      sid,
      file.name,
      file.type || "application/octet-stream",
      btoa(binary),
    );
    await store.refreshSession(sid);
  } catch (e) {
    store.fail(e);
  } finally {
    uploading.value = false;
    input.value = "";
  }
}
async function removeAttachment(id: string) {
  try {
    await api.deleteAttachment(store.selectedSession, id);
    await store.refreshSession(store.selectedSession);
  } catch (e) {
    store.fail(e);
  }
}
async function abandon() {
  if (!confirm("放弃本轮恢复？历史将保留，未确认副作用仍需人工检查。")) return;
  try {
    await api.abandon(store.selectedSession);
    await store.refreshSession(store.selectedSession);
  } catch (e) {
    store.fail(e);
  }
}
</script>
<template>
  <section class="chat-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">WORKSPACE / CONVERSATION</p>
        <h1>
          {{
            session?.title === "New session"
              ? "新对话"
              : session?.title || "开始一次工程对话"
          }}
        </h1>
        <p class="muted">
          {{ session?.working_directory || "从左侧添加项目并创建对话。" }}
        </p>
      </div>
      <span v-if="run" class="badge" :class="run.status">{{
        run.status === "running"
          ? "运行中"
          : run.status === "waiting_for_user"
            ? "等待审批"
            : run.status === "completed"
              ? "已完成"
              : run.status === "interrupted"
                ? "已停驻"
                : run.status === "failed"
                  ? "失败"
                  : run.status
      }}</span>
    </header>
    <div v-if="!session" class="empty-state">
      <div class="empty-symbol">◒</div>
      <h2>把项目带进来，一起把问题解决</h2>
      <p>分析代码、定位故障、检查工具证据，最后由你决定采纳哪些修改。</p>
      <button
        v-if="store.selectedWorkspace"
        class="primary"
        @click="store.createSession().catch(store.fail)"
      >
        新建对话
      </button>
    </div>
    <template v-else
      ><div class="conversation" aria-label="对话记录">
        <template
          v-for="(message, index) in store.messages[store.selectedSession] ||
          []"
          :key="index"
          ><article
            v-if="content(message)"
            class="message"
            :class="message.role"
          >
            <div class="message-author">
              {{
                message.role === "user"
                  ? "你"
                  : message.role === "assistant"
                    ? "Eventide"
                    : message.role
              }}
            </div>
            <SafeContent :text="content(message)" /></article
        ></template>
        <div
          v-if="!store.messages[store.selectedSession]?.length"
          class="empty-state compact"
        >
          <h2>这次想完成什么？</h2>
          <p>可以先让 Agent 只读分析项目，再决定修改范围。</p>
        </div>
        <article v-if="preview?.active" class="message assistant preview">
          <div class="message-author">
            Eventide <span class="badge">生成中 · 尚未保存</span>
          </div>
          <p v-if="preview.omitted_chars" class="muted">只显示最近的预览内容</p>
          <SafeContent :text="preview.text" />
        </article>
        <section
          v-for="(approval, id) in run?.pending_approvals || {}"
          :key="id"
          class="approval-card"
          role="region"
          aria-label="工具审批"
        >
          <h3>需要你的批准</h3>
          <p>{{ approval.reason || "工具请求了风险操作。" }}</p>
          <pre>{{ JSON.stringify(approval, null, 2) }}</pre>
          <div class="button-row">
            <button
              class="primary"
              @click="store.approve(String(id), true).catch(store.fail)"
            >
              批准这一次</button
            ><button
              @click="store.approve(String(id), false).catch(store.fail)"
            >
              拒绝
            </button>
          </div>
        </section>
        <div v-if="run?.status === 'interrupted'" class="parked-card">
          <strong>{{ unknown ? "工具结果未确认" : "本轮已停驻" }}</strong>
          <p>
            {{ run.reason || "执行已中断" }} ·
            {{ run.error || "恢复前将重新检查源码及工具状态。" }}
          </p>
          <div class="button-row">
            <button
              v-if="!unknown"
              @click="store.continueRun().catch(store.fail)"
            >
              检查并继续</button
            ><button v-if="!unknown" @click="abandon">放弃恢复</button
            ><span v-else
              >请检查原始工作副本；该状态不允许直接继续或采纳。</span
            >
          </div>
        </div>
        <details v-if="run" class="run-details">
          <summary>
            运行证据 · {{ run.steps || 0 }} 步 ·
            {{ run.tool_calls || 0 }} 次工具调用
          </summary>
          <a
            :href="`/api/runs/${encodeURIComponent(run.run_id)}/export`"
            download
            >下载本轮事件记录</a
          >
          <pre>{{ JSON.stringify(run.tools || {}, null, 2) }}</pre>
          <ul>
            <li v-for="task in session.task_plan || []" :key="task.id">
              {{ task.id }} · {{ task.content }} · {{ task.status
              }}<small v-if="task.summary">
                — {{ task.summary }}（模型声明）</small
              >
            </li>
          </ul>
        </details>
      </div>
      <form class="composer" @submit.prevent="submit">
        <div v-if="!store.provider?.api_key_configured" class="composer-notice">
          尚未配置模型连接。<RouterLink to="/settings">前往设置</RouterLink>
        </div>
        <div v-if="busyOther" class="composer-notice">
          同一源工作副本有任务运行中。当前后端暂时限制并发，请等待结束。
        </div>
        <div v-if="pending" class="composer-notice">
          上次请求的接收结果待确认。<button
            type="button"
            @click="store.retryPending().catch(store.fail)"
          >
            使用同一身份重试
          </button>
        </div>
        <div class="attachment-chips">
          <button
            v-for="file in store.attachments[store.selectedSession] || []"
            :key="file.attachment_id"
            type="button"
            @click="removeAttachment(file.attachment_id)"
          >
            {{ file.name }} ×
          </button>
        </div>
        <textarea
          v-model="draft"
          aria-label="任务输入"
          placeholder="描述目标、需要检查的代码，或你遇到的问题…"
          rows="3"
          :disabled="!!pending || store.creating"
          @keydown.ctrl.enter.prevent="submit"
          @keydown.meta.enter.prevent="submit"
        ></textarea>
        <div class="composer-toolbar">
          <div class="button-row">
            <select v-model="mode" aria-label="运行模式">
              <option value="auto">Auto · 风险操作询问</option>
              <option value="plan">Plan · 只读分析</option>
              <option value="agent">Agent · 自动批准风险操作</option></select
            ><select v-model="model" aria-label="本次模型">
              <option v-for="item in models" :key="item" :value="item">
                {{ item }}
              </option></select
            ><label class="file-button"
              >{{ uploading ? "上传中…" : "＋ 附件"
              }}<input
                type="file"
                :disabled="uploading || isActive"
                @change="upload"
            /></label>
          </div>
          <button
            v-if="isActive"
            class="stop-button"
            type="button"
            @click="store.cancel().catch(store.fail)"
          >
            ■ 停止</button
          ><button
            v-else
            class="primary"
            type="submit"
            :disabled="
              !draft.trim() ||
              busyOther ||
              session.status === 'parked' ||
              !!pending ||
              store.sending[store.selectedSession] ||
              !store.provider?.api_key_configured
            "
          >
            {{ store.sending[store.selectedSession] ? "接收中…" : "发送 ↑" }}
          </button>
        </div>
        <details class="generation-panel">
          <summary>本次生成设置</summary>
          <div class="button-row">
            <label
              >模型标识
              <input
                v-model="model"
                maxlength="256"
                aria-label="手动模型标识" /></label
            ><button type="button" @click="refreshModels">
              联网刷新模型列表
            </button>
          </div>
          <GenerationEditor
            :key="`${store.selectedSession}:${model}`"
            :model="model"
            :session="store.selectedSession"
            @change="generation = $event"
          />
        </details>
        <p v-if="mode === 'agent'" class="inline-warning">
          Agent 模式会自动批准本轮风险工具，请确认项目与任务范围。
        </p>
        <small class="muted"
          >⌘ / Ctrl + Enter 发送 · 切换页面不会停止运行 · 本地 Shell
          不是安全沙箱</small
        >
      </form>
    </template>
  </section>
</template>
