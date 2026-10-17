<script setup lang="ts">
import PanelClose from "../components/PanelClose.vue";
import { useUnsavedChanges } from "../composables/useUnsavedChanges";
import { computed, ref, watch, onUnmounted } from "vue";
import {
  skillsApi,
  type SkillStatus,
  type PackagePreview,
  type PackageInput,
  type SkillConfiguration,
  type SkillPackage,
  type RunSkills,
  type SkillDiff,
} from "../api/skills";
import { useWorkbench } from "../stores/workbench";
const store = useWorkbench();
const state = ref<SkillStatus | null>(null),
  bundles = ref<PackagePreview[]>([]),
  error = ref(""),
  notice = ref(""),
  busy = ref(false);
const config = ref<SkillConfiguration>({
  enabled_user: {},
  disabled_project: [],
  selected: [],
});
const canDiscard = useUnsavedChanges(
  () =>
    !!state.value &&
    JSON.stringify(config.value) !==
      JSON.stringify({
        enabled_user: state.value.configuration.enabled_user,
        disabled_project: state.value.configuration.disabled_project || [],
        selected: state.value.configuration.selected || [],
      }),
);
const source = ref("bundled"),
  bundled = ref("code-review"),
  directory = ref(""),
  archive = ref(""),
  scope = ref<"user" | "project">("user");
const preview = ref<PackagePreview | null>(null),
  run = ref<RunSkills | null>(null),
  diff = ref<SkillDiff | null>(null);
const inspected = ref<SkillPackage | null>(null),
  resource = ref("SKILL.md"),
  content = ref(""),
  offset = ref(0);
let revision = 0,
  inputRevision = 0,
  runRevision = 0,
  resourceRevision = 0;
const names = computed(() => [
  ...new Set(state.value?.installed.map((p) => p.name) || []),
]);
const available = computed(() => [
  ...(state.value?.project_packages.skills
    .filter((p) => !config.value.disabled_project?.includes(p.skill_id))
    .map((p) => p.skill_id) || []),
  ...Object.keys(config.value.enabled_user || {}).map((name) => `user:${name}`),
]);
const diagnostics = computed(() => state.value?.enabled.diagnostics || []);
function fail(e: unknown) {
  error.value = e instanceof Error ? e.message : "操作未完成";
}
async function refresh(workspace = store.selectedWorkspace) {
  const current = ++revision;
  try {
    const value = await skillsApi.status(workspace);
    if (current !== revision || workspace !== store.selectedWorkspace) return;
    state.value = value;
    config.value = {
      enabled_user: { ...value.configuration.enabled_user },
      disabled_project: [...(value.configuration.disabled_project || [])],
      selected: [...(value.configuration.selected || [])],
    };
  } catch (e) {
    if (current === revision) fail(e);
  }
}
watch(
  () => store.selectedWorkspace,
  (workspace) => {
    revision++;
    inputRevision++;
    resourceRevision++;
    state.value = null;
    preview.value = null;
    inspected.value = null;
    diff.value = null;
    content.value = "";
    error.value = "";
    notice.value = "";
    if (workspace) void refresh(workspace);
  },
  { immediate: true },
);
void skillsApi
  .bundled()
  .then((value) => (bundles.value = value))
  .catch(fail);
watch([source, bundled, directory, archive, scope], () => {
  inputRevision++;
  preview.value = null;
});
watch(
  () => [
    store.currentRun?.run_id,
    store.currentRun?.status,
    store.events[store.currentRun?.run_id || ""]?.length,
  ],
  async () => {
    const current = ++runRevision,
      identity = store.currentRun?.run_id;
    run.value = null;
    if (identity)
      try {
        const value = await skillsApi.run(identity);
        if (current === runRevision) run.value = value;
      } catch (e) {
        if (current === runRevision) fail(e);
      }
  },
  { immediate: true },
);
onUnmounted(() => {
  revision++;
  inputRevision++;
  runRevision++;
  resourceRevision++;
});
function body(): PackageInput {
  return {
    scope: scope.value,
    ...(source.value === "bundled"
      ? { bundled: bundled.value }
      : source.value === "directory"
        ? { directory: directory.value }
        : { archive_base64: archive.value }),
  };
}
async function upload(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  archive.value = "";
  preview.value = null;
  if (!file) return;
  if (file.size > 8_000_000) {
    error.value = "ZIP 最大为 8 MB";
    return;
  }
  const current = ++inputRevision;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 8192)
      binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    if (current === inputRevision) archive.value = btoa(binary);
  } catch (e) {
    if (current === inputRevision) fail(e);
  }
}
async function review() {
  if (busy.value || !store.selectedWorkspace) return;
  const workspace = store.selectedWorkspace,
    current = inputRevision;
  busy.value = true;
  error.value = "";
  notice.value = "";
  preview.value = null;
  try {
    const value = await skillsApi.preview(workspace, body());
    if (workspace === store.selectedWorkspace && current === inputRevision)
      preview.value = value;
  } catch (e) {
    if (workspace === store.selectedWorkspace && current === inputRevision)
      fail(e);
  } finally {
    busy.value = false;
  }
}
async function install() {
  if (busy.value || !preview.value?.valid) return;
  const workspace = store.selectedWorkspace,
    current = inputRevision;
  const request = { ...body(), expected_version: preview.value.version };
  busy.value = true;
  error.value = "";
  try {
    await skillsApi.import(workspace, request);
    if (workspace === store.selectedWorkspace && current === inputRevision) {
      notice.value =
        request.scope === "project"
          ? "已导入项目目录，后续运行可发现；请用 Git 管理更新。"
          : "已安装固定版本；在下方为当前项目启用后才会提供给模型。";
      preview.value = null;
      await refresh(workspace);
    }
  } catch (e) {
    if (workspace === store.selectedWorkspace && current === inputRevision)
      fail(e);
  } finally {
    busy.value = false;
  }
}
function pin(name: string, version: string) {
  config.value.enabled_user ||= {};
  if (version) config.value.enabled_user[name] = version;
  else delete config.value.enabled_user[name];
  config.value.selected = config.value.selected?.filter((identity) =>
    available.value.includes(identity),
  );
}
function toggleProject(identity: string, enabled: boolean) {
  config.value.disabled_project = (config.value.disabled_project || []).filter(
    (v) => v !== identity,
  );
  if (!enabled) config.value.disabled_project.push(identity);
  config.value.selected = config.value.selected?.filter((v) =>
    available.value.includes(v),
  );
}
async function save() {
  if (busy.value || !state.value) return;
  const workspace = store.selectedWorkspace;
  busy.value = true;
  error.value = "";
  try {
    await skillsApi.configure(workspace, {
      ...config.value,
      selected: config.value.selected?.filter((identity) =>
        available.value.includes(identity),
      ),
      expected_configuration_hash: state.value.configuration_hash,
    });
    if (workspace === store.selectedWorkspace) {
      notice.value = "配置已保存，应用于后续运行；在途运行保留原版本。";
      await refresh(workspace);
    }
  } catch (e) {
    if (workspace === store.selectedWorkspace) fail(e);
  } finally {
    busy.value = false;
  }
}
async function inspect(p: SkillPackage) {
  inspected.value = p;
  resource.value = "SKILL.md";
  offset.value = 0;
  diff.value = null;
  await read();
}
async function read() {
  const p = inspected.value,
    current = ++resourceRevision;
  content.value = "";
  if (!p) return;
  try {
    const value = await skillsApi.resource(
      p.name,
      p.version,
      resource.value,
      offset.value,
    );
    if (current === resourceRevision) content.value = value.content;
  } catch (e) {
    if (current === resourceRevision) fail(e);
  }
}
async function compare(p: SkillPackage) {
  const before = state.value?.configuration.enabled_user?.[p.name];
  if (!before) return;
  const current = ++resourceRevision;
  inspected.value = p;
  content.value = "";
  diff.value = null;
  try {
    const value = await skillsApi.diff(p.name, before, p.version);
    if (current === resourceRevision) diff.value = value;
  } catch (e) {
    if (current === resourceRevision) fail(e);
  }
}
</script>

<template>
  <section class="page skills-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">WORKSPACE / SKILLS</p>
        <h1>Skills 扩展</h1>
        <p class="muted">按项目启用与选择，运行时按需加载固定版本。</p>
      </div>
      <button
        :disabled="busy || !store.selectedWorkspace"
        @click="canDiscard() && refresh()"
      >
        刷新状态
      </button>
    </header>
    <p v-if="error" role="alert" class="inline-error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="!store.selectedWorkspace" class="empty-state">
      先添加或选择项目。
    </p>
    <template v-else>
      <section class="panel">
        <h2>导入 Skill</h2>
        <p class="muted">
          安装只保存资源。依赖由你准备，脚本执行仍须通过当前运行的权限检查。
        </p>
        <fieldset :disabled="busy" class="skill-import">
          <label
            >来源<select v-model="source" aria-label="Skill 来源">
              <option value="bundled">内置包</option>
              <option value="directory">服务所在主机的目录</option>
              <option value="zip">上传 ZIP</option>
            </select></label
          >
          <label v-if="source === 'bundled'"
            >内置包<select v-model="bundled" aria-label="内置 Skill">
              <option v-for="p in bundles" :key="p.name" :value="p.name">
                {{ p.name }}
              </option>
            </select></label
          >
          <label v-else-if="source === 'directory'"
            >目录路径<input
              v-model="directory"
              aria-label="Skill 目录"
              placeholder="/path/to/skill"
          /></label>
          <label v-else
            >ZIP 文件<input
              type="file"
              accept=".zip"
              aria-label="Skill ZIP"
              @change="upload"
          /></label>
          <label
            >导入位置<select v-model="scope" aria-label="导入作用域">
              <option value="user">用户级：安装后按项目启用</option>
              <option value="project">项目级：写入项目 skills 目录</option>
            </select></label
          >
          <button @click="review">检查导入内容</button>
        </fieldset>
        <div v-if="preview" class="skill-preview">
          <PanelClose
            label="关闭安装预览"
            @close="
              inputRevision++;
              preview = null;
            "
          />
          <h3>{{ preview.name || "无效包" }}</h3>
          <p>{{ preview.description }}</p>
          <code>{{ preview.version }}</code>
          <ul>
            <li v-for="d in preview.diagnostics" :key="`${d.code}:${d.source}`">
              {{ d.severity }} · {{ d.message }}
            </li>
          </ul>
          <details>
            <summary>资源清单（{{ preview.manifest.length }}）</summary>
            <ul>
              <li v-for="r in preview.manifest" :key="r.path">
                {{ r.path }} · {{ r.size }} 字节
              </li>
            </ul>
          </details>
          <button
            class="primary"
            :disabled="busy || !preview.valid"
            @click="install"
          >
            导入已检查版本
          </button>
        </div>
      </section>
      <section v-if="state" class="panel">
        <h2>当前项目配置</h2>
        <p class="muted">
          用户包可以固定或回退到已安装版本；项目包由源码和 Git
          管理。选择会提示模型优先使用，实际加载另有记录。
        </p>
        <ul v-if="diagnostics.length" class="inline-error">
          <li v-for="d in diagnostics" :key="`${d.code}:${d.source}`">
            {{ d.source }}：{{ d.message }}
          </li>
        </ul>
        <fieldset :disabled="busy">
          <div v-for="name in names" :key="name" class="skill-config-row">
            <label :for="`pin-${name}`">user:{{ name }} · 已安装</label>
            <select
              :id="`pin-${name}`"
              :aria-label="`${name} 固定版本`"
              :value="config.enabled_user?.[name] || ''"
              @change="pin(name, ($event.target as HTMLSelectElement).value)"
            >
              <option value="">未启用</option>
              <option
                v-for="p in state.installed.filter((p) => p.name === name)"
                :key="p.version"
                :value="p.version"
              >
                {{ p.version.slice(0, 12) }}
              </option>
            </select>
          </div>
          <label
            v-for="p in state.project_packages.skills"
            :key="p.skill_id"
            class="skill-check"
            ><input
              type="checkbox"
              :checked="!config.disabled_project?.includes(p.skill_id)"
              @change="
                toggleProject(
                  p.skill_id,
                  ($event.target as HTMLInputElement).checked,
                )
              "
            />启用 {{ p.skill_id }}
            <code>{{ p.version.slice(0, 12) }}</code></label
          >
          <p v-if="!names.length && !state.project_packages.skills.length">
            尚无可用 Skill。
          </p>
          <h3>主动选择</h3>
          <label
            v-for="identity in available"
            :key="identity"
            class="skill-check"
            ><input
              v-model="config.selected"
              type="checkbox"
              :value="identity"
            />选择 {{ identity }}</label
          >
          <button class="primary" @click="save">保存项目配置</button>
        </fieldset>
        <p class="muted">
          服务端已启用 {{ state.enabled.skills.length }} 个，已选中
          {{ state.enabled.selected.length }} 个。
        </p>
      </section>
      <section v-if="state?.installed.length" class="panel">
        <h2>已安装版本与资源</h2>
        <article
          v-for="p in state.installed"
          :key="`${p.name}:${p.version}`"
          class="skill-version"
        >
          <div>
            <strong>{{ p.name }}</strong>
            <code>{{ p.version.slice(0, 12) }}</code>
            <p class="muted">{{ p.description }}</p>
          </div>
          <div class="button-row">
            <button @click="inspect(p)">查看资源</button
            ><button
              :disabled="!state.configuration.enabled_user?.[p.name]"
              @click="compare(p)"
            >
              与已启用版本比较
            </button>
          </div>
        </article>
        <div v-if="inspected" class="skill-preview">
          <PanelClose
            label="关闭 Skill 详情"
            @close="
              resourceRevision++;
              inspected = null;
              diff = null;
              content = '';
            "
          />
          <h3>{{ inspected.name }} · {{ inspected.version.slice(0, 12) }}</h3>
          <template v-if="diff"
            ><p>
              新增：{{ diff.added.join(", ") || "无" }}；移除：{{
                diff.removed.join(", ") || "无"
              }}；改变：{{ diff.changed.join(", ") || "无" }}
            </p>
            <pre>{{ diff.instructions_diff || "指令正文无变化" }}</pre>
            <p v-if="diff.diff_truncated">差异已截断。</p></template
          >
          <template v-else
            ><select
              v-model="resource"
              aria-label="Skill 资源"
              @change="
                offset = 0;
                read();
              "
            >
              <option v-for="r in inspected.manifest" :key="r.path">
                {{ r.path }}
              </option>
            </select>
            <pre>{{ content }}</pre>
            <div class="button-row">
              <button
                :disabled="offset === 0"
                @click="
                  offset = Math.max(0, offset - 12000);
                  read();
                "
              >
                上一段</button
              ><span>字符偏移 {{ offset }}</span
              ><button
                @click="
                  offset += 12000;
                  read();
                "
              >
                下一段
              </button>
            </div></template
          >
        </div>
      </section>
      <section class="panel">
        <h2>当前运行的 Skill 记录</h2>
        <template v-if="run"
          ><p>运行 {{ run.run_id }}</p>
          <p>固定选择：{{ run.snapshot?.selected.join(", ") || "无" }}</p>
          <p v-if="!run.loaded.length">尚无实际加载记录。</p>
          <ul>
            <li v-for="item in run.loaded" :key="item.seq">
              已加载 {{ item.skill_id }} · {{ item.version.slice(0, 12) }} ·
              正文 {{ item.characters }} 字符（不是 Token 用量）
            </li>
          </ul></template
        >
        <p v-else class="muted">选择一个已有运行的对话后查看。</p>
      </section>
    </template>
  </section>
</template>

<style scoped>
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.skill-import {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  align-items: end;
}
.skill-import label {
  display: grid;
  gap: 8px;
}
.skill-config-row {
  display: flex;
  gap: 16px;
  align-items: center;
  margin: 14px 0;
  flex-wrap: wrap;
}
.skill-check {
  display: flex;
  gap: 10px;
  align-items: center;
  margin: 12px 0;
}
.skill-check input {
  width: auto;
}
.skill-version {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 14px;
  border-bottom: 1px solid var(--line);
  padding: 16px 0;
}
.skill-preview {
  margin-top: 20px;
  padding: 16px;
  background: var(--soft);
  border-radius: 12px;
}
.skill-preview pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 500px;
  overflow: auto;
}
code {
  overflow-wrap: anywhere;
}
.button-row {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
</style>
