<script setup lang="ts">
import ModelRetryNotice from "../components/ModelRetryNotice.vue";
import ConnectionNotice from "../components/ConnectionNotice.vue";
import IndexPreparationOptions from "../components/IndexPreparationOptions.vue";
import { computed, nextTick, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useWorkbench } from "../stores/workbench";
import { api } from "../api/client";
import type { Generation, Message } from "../api/types";
import GenerationEditor from "../components/GenerationEditor.vue";
import SafeContent from "../components/SafeContent.vue";
import SandboxEvidence from "../components/SandboxEvidence.vue";
import RunMemoryPanel from "../components/RunMemoryPanel.vue";
import InputManifestPanel from "../components/InputManifestPanel.vue";
import RunSupplementPanel from "../components/RunSupplementPanel.vue";
import RunToolsPanel from "../components/RunToolsPanel.vue";
import EventideLogo from "../components/EventideLogo.vue";
import ToolActivity from "../components/ToolActivity.vue";
import RetrievalStatus from "../components/RetrievalStatus.vue";
import ComposerMenu from "../components/ComposerMenu.vue";
import { skillsApi, type SkillStatus } from "../api/skills";
const store = useWorkbench(),
  route = useRoute(),
  mode = ref("auto"),
  executionTarget = ref<"source" | "snapshot" | "worktree">("source"),
  executionProfile = ref<"local" | "sandbox">("local"),
  sandboxImage = ref(""),
  prepareIndex = ref(false),
  structuredIndex = ref(false),
  indexScope = ref<"code" | "all">("code"),
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
const boundWorktree = computed(
  () => session.value?.execution?.kind === "worktree",
);
const boundSandbox = computed(
  () =>
    session.value?.execution_profile === "sandbox" ||
    session.value?.execution?.kind === "sandbox" ||
    run.value?.execution?.kind === "sandbox",
);
const profileFrozen = computed(
  () =>
    !!session.value?.execution_profile ||
    !!session.value?.latest_run ||
    !!run.value ||
    boundWorktree.value ||
    boundSandbox.value,
);
watch(
  [() => store.selectedSession, boundSandbox, profileFrozen],
  () => {
    const pendingBody = store.pending[store.selectedSession]?.body;
    executionProfile.value =
      boundSandbox.value ||
      (!profileFrozen.value && pendingBody?.execution_profile === "sandbox")
        ? "sandbox"
        : "local";
    sandboxImage.value = boundSandbox.value
      ? session.value?.sandbox_profile?.requested_image ||
        session.value?.execution?.profile?.requested_image ||
        ""
      : pendingBody?.sandbox_image || "";
  },
  { immediate: true },
);
const evaluationRun = computed(() =>
  session.value?.execution?.kind === "evaluation"
    ? session.value.execution.evaluation_id || run.value?.run_id
    : undefined,
);
const busyOther = computed(
    () =>
      store.workspaceBusy &&
      !isActive.value &&
      !boundWorktree.value &&
      !boundSandbox.value,
  ),
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
watch(
  () => store.selectedSession,
  () => {
    mode.value = "auto";
  },
  { flush: "sync" },
);
watch(
  [() => store.selectedSession, () => session.value?.execution?.kind],
  () => {
    const kind = session.value?.execution?.kind;
    executionTarget.value =
      kind === "worktree" || kind === "snapshot" ? kind : "source";
    if (executionTarget.value === "snapshot") mode.value = "plan";
  },
  { immediate: true },
);
const canPrepareIndex = computed(
  () =>
    executionProfile.value === "local" && executionTarget.value !== "snapshot",
);
watch(
  () => store.selectedSession,
  () => {
    prepareIndex.value =
      store.pending[store.selectedSession]?.body.knowledge_index === "prepare";
    structuredIndex.value =
      store.pending[store.selectedSession]?.body.code_structure ?? false;
    indexScope.value =
      store.pending[store.selectedSession]?.body.source_scope ?? "code";
  },
  { immediate: true },
);
watch(canPrepareIndex, (supported) => {
  if (!supported) prepareIndex.value = false;
});
watch(mode, () => {
  if (mode.value !== "plan" && executionTarget.value === "snapshot")
    executionTarget.value = "source";
});
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
const canSubmit = computed(
  () =>
    !!draft.value.trim() &&
    (executionProfile.value !== "sandbox" ||
      boundSandbox.value ||
      !!sandboxImage.value.trim()) &&
    !!session.value &&
    !evaluationRun.value &&
    !isActive.value &&
    !busyOther.value &&
    session.value.status !== "parked" &&
    run.value?.status !== "interrupted" &&
    !pending.value &&
    !store.sending[store.selectedSession] &&
    !store.creating &&
    !uploading.value &&
    !!store.provider?.api_key_configured,
);
const composing = ref(false),
  inspector = ref(false),
  conversation = ref<HTMLElement | null>(null),
  readingOlder = ref(false);
const skills = ref<SkillStatus | null>(null),
  skillsError = ref("");
async function loadSkills() {
  const workspace = store.selectedWorkspace;
  if (!workspace) return;
  skillsError.value = "";
  try {
    const result = await skillsApi.status(workspace);
    if (workspace === store.selectedWorkspace) skills.value = result;
  } catch (e) {
    skillsError.value = e instanceof Error ? e.message : "无法读取 Skills";
  }
}
watch(
  () => store.selectedWorkspace,
  () => {
    skills.value = null;
    skillsError.value = "";
  },
);
function onKeydown(event: KeyboardEvent) {
  if (
    event.key === "Enter" &&
    (event.ctrlKey || event.metaKey) &&
    !event.isComposing &&
    !composing.value &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    void submit();
  }
}
function onScroll() {
  const el = conversation.value;
  if (el)
    readingOlder.value = el.scrollHeight - el.scrollTop - el.clientHeight > 80;
}
async function jumpLatest() {
  readingOlder.value = false;
  await nextTick();
  const el = conversation.value;
  if (el) el.scrollTop = el.scrollHeight;
}
watch(
  () => store.selectedSession,
  () => {
    readingOlder.value = false;
    void jumpLatest();
  },
);
watch(
  () => [
    store.messages[store.selectedSession],
    preview.value?.text,
    run.value?.tools,
    run.value?.status,
  ],
  async () => {
    if (!readingOlder.value) await jumpLatest();
  },
  { deep: true, flush: "post" },
);
async function submit() {
  if (!canSubmit.value) return;
  const identity = store.selectedSession;
  try {
    await store.submit({
      prompt: draft.value,
      mode: mode.value,
      ...(prepareIndex.value && canPrepareIndex.value
        ? {
            knowledge_index: "prepare" as const,
            code_structure: structuredIndex.value,
            source_scope: indexScope.value,
          }
        : {}),
      ...(executionProfile.value === "sandbox"
        ? {
            execution_profile: "sandbox" as const,
            sandbox_image: boundSandbox.value
              ? null
              : sandboxImage.value.trim(),
          }
        : {}),
      execution_target:
        executionProfile.value === "sandbox"
          ? "source"
          : boundWorktree.value
            ? "worktree"
            : executionTarget.value,
      model: model.value || null,
      generation: generation.value,
      attachment_ids: (store.attachments[store.selectedSession] || []).map(
        (a) => a.attachment_id,
      ),
    });
  } catch (e) {
    if (identity === store.selectedSession) store.fail(e);
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
    const target = store.currentRun;
    if (!target) return;
    await api.abandon(store.selectedSession, target.run_id);
    await store.refreshSession(store.selectedSession);
  } catch (e) {
    store.fail(e);
  }
}
</script>
<template>
  <section class="chat-page" :class="{ 'inspector-open': inspector }">
    <header class="page-heading">
      <div>
        <p class="eyebrow">CONVERSATION</p>
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
        <p v-if="boundWorktree" class="muted">
          本对话使用独立工作副本 · 后续消息与恢复沿用同一份修改
          <small
            >{{ session?.execution?.name }} ·
            {{ session?.execution?.base_commit?.slice(0, 12) }}</small
          >
        </p>
        <p v-if="run?.execution?.kind === 'snapshot'" class="muted">
          本轮使用固定只读快照 · 源码变化不影响在途读取
        </p>
      </div>
      <div class="chat-heading-actions">
        <button
          v-if="session"
          class="inspector-toggle"
          type="button"
          :aria-expanded="inspector"
          @click="inspector = !inspector"
        >
          上下文 <span aria-hidden="true">☷</span></button
        ><span v-if="run" class="badge" :class="run.status">{{
          run.status === "queued"
            ? "排队中"
            : run.status === "running"
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
      </div>
    </header>
    <div v-if="!session" class="empty-state">
      <div class="empty-symbol"><EventideLogo /></div>
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
    <section v-else-if="evaluationRun" class="panel">
      <h2>评测记录</h2>
      <p>在评测页面查看逐题结果、停止任务或发起新的批次。</p>
      <RouterLink
        :to="{ path: '/evaluations', query: { evaluation: evaluationRun } }"
        >打开评测</RouterLink
      >
    </section>
    <template v-else
      ><p v-if="run?.status === 'queued'" class="queue-notice">
        任务已持久接收，正在等待执行额度。<RouterLink to="/queue"
          >查看任务队列</RouterLink
        >
      </p>
      <div
        ref="conversation"
        class="conversation"
        aria-label="对话记录"
        tabindex="0"
        @scroll="onScroll"
      >
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
          <EventideLogo />
          <p class="welcome-eyebrow">A LITTLE CLARITY. A LOT OF POSSIBILITY.</p>
          <h2>这次想完成什么？</h2>
          <p>可以先让 Agent 只读分析项目，再决定修改范围。</p>
        </div>
        <ConnectionNotice
          v-if="
            isActive &&
            !['connected', 'closed'].includes(store.eventConnection.state)
          "
          :status="store.eventConnection"
        />
        <ModelRetryNotice
          v-if="isActive && run?.model_retry"
          :retry="run.model_retry"
        />
        <RunSupplementPanel
          v-if="run"
          :run-id="run.run_id"
          :run-status="run.status"
          :refresh-key="`${run.status}:${store.events[run.run_id]?.length || 0}`"
        />
        <RunMemoryPanel
          v-if="run"
          :run-id="run.run_id"
          :refresh-key="`${run.status}:${store.events[run.run_id]?.length || 0}`"
        />
        <InputManifestPanel v-if="run" :run-id="run.run_id" />
        <RunToolsPanel
          v-if="run"
          :run-id="run.run_id"
          :refresh-key="`${run.status}:${store.events[run.run_id]?.length || 0}`"
        />
        <RetrievalStatus v-if="run" :state="run.retrieval" />
        <ToolActivity
          v-if="run"
          :tools="run.tools || {}"
          :session-id="store.selectedSession"
          :count="run.tool_calls"
          :active="isActive"
        />
        <p
          v-if="isActive && !preview?.active && !run?.model_retry"
          class="working-narrative"
          role="status"
        >
          <span class="working-dot"></span
          >{{
            run?.status === "queued"
              ? "已接收，等待执行额度…"
              : run?.status === "waiting_for_user"
                ? "等待你的审批，确认后继续。"
                : "正在处理任务，工具活动会显示在这里…"
          }}
        </p>
        <section
          v-if="run?.status === 'failed'"
          class="run-failure"
          role="status"
        >
          <strong>这次运行未能完成</strong>
          <p>{{ run.error || run.reason || "打开运行证据查看失败详情。" }}</p>
          <RouterLink to="/history">查看运行历史</RouterLink>
        </section>
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
          <span class="eyebrow">CHECKPOINT</span>
          <h3>{{ unknown ? "工具结果未确认" : "已停驻，等待你决定下一步" }}</h3>
          <p>
            Continue
            会重新检查源码与工具状态，并沿用本轮配置继续；不会自动恢复或重放未知操作。
          </p>
          <p>
            {{ run.reason || "执行已中断" }} ·
            {{ run.error || "恢复前将重新检查源码及工具状态。" }}
          </p>
          <div class="button-row">
            <button
              v-if="!unknown"
              @click="store.continueRun().catch(store.fail)"
            >
              Continue · 检查并继续</button
            ><button v-if="!unknown" @click="abandon">放弃恢复</button
            ><RouterLink
              v-if="run"
              :to="`/runs/${run.run_id}/recovery`"
              class="recovery-link"
              >查看恢复详情</RouterLink
            ><span v-if="unknown"
              >请检查原始工作副本；该状态不允许直接继续或采纳。</span
            >
          </div>
        </div>
        <SandboxEvidence
          v-if="run && boundSandbox"
          :execution="run.execution"
          :run-id="run.run_id"
        />
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
      <button
        v-if="readingOlder"
        class="jump-latest"
        type="button"
        @click="jumpLatest"
      >
        ↓ 回到最新
      </button>
      <form class="composer" @submit.prevent="submit">
        <div v-if="!store.provider?.api_key_configured" class="composer-notice">
          尚未配置模型连接。<RouterLink to="/settings">前往设置</RouterLink>
        </div>
        <div v-if="busyOther" class="composer-notice">
          源工作副本正在使用中，暂不能开始源任务或准备新副本。已绑定的工作副本和已接收的快照可独立运行。
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
          @keydown="onKeydown"
          @compositionstart="composing = true"
          @compositionend="composing = false"
        ></textarea>
        <div class="composer-toolbar">
          <div class="button-row composer-options">
            <select
              v-model="mode"
              :disabled="store.creating"
              aria-label="运行模式"
            >
              <option value="auto">Auto · 风险操作询问</option>
              <option value="plan">Plan · 只读分析</option>
              <option value="agent">Agent · 自动批准风险操作</option>
            </select>
            <select
              v-model="executionProfile"
              aria-label="执行环境"
              :disabled="
                profileFrozen || !!pending || isActive || store.creating
              "
            >
              <option value="local">本机执行</option>
              <option value="sandbox">Docker 沙箱 · 离线</option>
            </select>
            <select
              v-if="executionProfile === 'local'"
              v-model="executionTarget"
              :disabled="boundWorktree || store.creating"
              aria-label="执行位置"
            >
              <option value="source">当前源码 · 独占执行</option>
              <option v-if="mode === 'plan'" value="snapshot">
                固定快照 · 可并发
              </option>
              <option value="worktree">独立工作副本 · 可并发</option>
            </select>
            <select
              v-model="model"
              :disabled="store.creating"
              aria-label="本次模型"
            >
              <option v-for="item in models" :key="item" :value="item">
                {{ item }}
              </option></select
            ><ComposerMenu v-if="executionProfile === 'local'" label="Skills"
              ><div @vue:mounted="loadSkills">
                <strong>项目 Skills</strong>
                <p class="muted">已配置的技能由后端固定到本轮，按需加载。</p>
                <p v-if="skillsError" class="inline-error">{{ skillsError }}</p>
                <ul
                  v-if="skills?.enabled.skills.length"
                  class="composer-skill-list"
                >
                  <li
                    v-for="skill in skills.enabled.skills"
                    :key="skill.skill_id"
                  >
                    <strong>{{ skill.name }}</strong
                    ><small>{{ skill.description }}</small>
                  </li>
                </ul>
                <p v-else-if="skills" class="muted">当前没有启用的 Skills。</p>
                <button v-else type="button" @click="loadSkills">
                  读取 Skills</button
                ><RouterLink to="/skills">管理 Skills →</RouterLink>
              </div></ComposerMenu
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
            {{ run?.status === "queued" ? "取消排队" : "■ 停止" }}</button
          ><button v-else class="primary" type="submit" :disabled="!canSubmit">
            {{ store.sending[store.selectedSession] ? "接收中…" : "发送 ↑" }}
          </button>
        </div>
        <label class="knowledge-option"
          ><input
            v-model="prepareIndex"
            type="checkbox"
            aria-label="任务开始前准备知识索引"
            :disabled="
              !canPrepareIndex || !!pending || isActive || store.creating
            "
          />任务开始前准备知识索引</label
        >
        <IndexPreparationOptions
          v-if="prepareIndex"
          v-model:structured="structuredIndex"
          v-model:scope="indexScope"
          :disabled="!!pending || isActive || store.creating"
        />
        <p class="muted">
          {{
            canPrepareIndex
              ? "只准备本次实际副本；未勾选时只用已有匹配索引。远程构建和语义查询可能产生用量。"
              : "固定快照与 Docker 任务沙箱暂不支持知识索引准备。"
          }}
        </p>
        <div v-if="executionProfile === 'sandbox'" class="sandbox-options">
          <label v-if="!boundSandbox"
            >预装依赖的本地 Docker 镜像
            <input
              v-model="sandboxImage"
              aria-label="沙箱镜像"
              placeholder="例如 eventide-dev:local"
              maxlength="256"
              :disabled="!!pending || isActive || store.creating"
            />
          </label>
          <p v-else class="muted">
            本对话已固定沙箱镜像与候选目录，后续消息和 Continue 沿用。
          </p>
          <p class="muted">
            容器工具离线执行，模型仍由本机连接服务商。仅使用受信项目和镜像；复制过滤后的文本文件，排除
            .git、常见凭据文件和二进制，但不扫描源码中的秘密。请提前准备镜像及依赖。Docker
            不可用或镜像不存在时会明确失败，不会回退到本机执行。
          </p>
          <p class="muted">
            沙箱 v1 不启用 MCP、Skills、workers
            或检索。修改保留在候选目录中，只能显式导出补丁，不会自动采纳到源码。选择沙箱不代表
            Docker 或镜像已就绪。
          </p>
        </div>
        <p v-if="profileFrozen" class="muted">
          执行环境已随首轮固定；切换本机或沙箱请新建对话。
        </p>
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
        <p
          v-if="
            executionProfile === 'local' &&
            mode === 'plan' &&
            executionTarget === 'snapshot'
          "
          class="muted"
        >
          需要 Git 项目和可用
          HEAD。快照排除密钥、链接、二进制及过大文件；本轮使用固定
          Skills，不连接 MCP 或读取源目录索引。Continue 继续使用原快照。
        </p>
        <p
          v-if="executionProfile === 'local' && executionTarget === 'worktree'"
          class="muted"
        >
          首次创建需要干净的 Git 源项目和可用
          HEAD；修改保留在本对话副本中，不自动提交或采纳。
          绑定后不会切回源目录，若要操作其他副本请新建对话。工作副本不是操作系统沙箱。
        </p>
        <small class="muted"
          >Enter 换行 · ⌘ / Ctrl + Enter 发送 · 草稿不会自动排队 · 本地 Shell
          不是安全沙箱</small
        >
      </form>
    </template>
    <Transition name="inspector"
      ><aside
        v-if="inspector && session"
        class="context-inspector"
        aria-label="对话上下文"
      >
        <div class="inspector-heading">
          <h2>上下文</h2>
          <button
            type="button"
            class="icon-button"
            aria-label="关闭上下文"
            @click="inspector = false"
          >
            ×
          </button>
        </div>
        <section>
          <h3>项目目录</h3>
          <p class="mono">{{ session.working_directory }}</p>
          <h3>执行位置</h3>
          <p>{{ session.execution?.kind || "source" }}</p>
          <p v-if="session.execution?.name">{{ session.execution.name }}</p>
          <p v-if="boundWorktree" class="muted">
            本对话沿用独立工作副本；不会自动提交或采纳。
          </p>
        </section>
        <section v-if="session.task_plan?.length">
          <h3>任务计划</h3>
          <ol>
            <li v-for="task in session.task_plan" :key="task.id">
              {{ task.content
              }}<small
                >{{ task.status }} ·
                {{ task.summary || "模型声明，非独立验收" }}</small
              >
            </li>
          </ol>
        </section>
        <section v-if="run">
          <h3>本轮运行</h3>
          <p class="mono">{{ run.run_id }}</p>
          <p>{{ run.steps || 0 }} 步 · {{ run.tool_calls || 0 }} 次工具调用</p>
          <RouterLink :to="{ path: '/usage', query: { run: run.run_id } }"
            >查看用量</RouterLink
          ><br /><a
            :href="`/api/runs/${encodeURIComponent(run.run_id)}/export`"
            download
            >下载事件记录</a
          >
        </section>

        <RouterLink to="/history">查看历史与证据 →</RouterLink>
      </aside></Transition
    >
  </section>
</template>

<style scoped>
.sandbox-options {
  margin-top: 12px;
}
.sandbox-options label {
  display: grid;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}
.sandbox-options input {
  width: 100%;
  min-width: 0;
}
</style>
