<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import {
  runToolsApi,
  type RunToolCatalog,
  type RunToolDetail,
  type RunToolStatusFilter,
} from "../api/runTools";

const props = defineProps<{
  runId: string;
  refreshKey?: string | number;
}>();

const PAGE_SIZE = 20;
const directory = ref<RunToolCatalog | null>(null),
  error = ref(""),
  loading = ref(false),
  query = ref(""),
  source = ref(""),
  status = ref<RunToolStatusFilter>("all"),
  offset = ref(0),
  expanded = ref<string | null>(null),
  detail = ref<RunToolDetail | null>(null),
  detailLoading = ref(false);
let version = 0,
  detailVersion = 0;

async function load() {
  const current = ++version,
    id = props.runId;
  if (!id) return;
  loading.value = true;
  error.value = "";
  try {
    const value = await runToolsApi.catalog(id, {
      query: query.value,
      source: source.value,
      status: status.value,
      offset: offset.value,
      limit: PAGE_SIZE,
    });
    if (current === version) directory.value = value;
  } catch (e) {
    if (current === version)
      error.value = e instanceof Error ? e.message : "工具目录读取失败";
  } finally {
    if (current === version) loading.value = false;
  }
}

function requery() {
  offset.value = 0;
  void load();
}

async function toggle(name: string) {
  const current = ++detailVersion;
  if (expanded.value === name) {
    expanded.value = null;
    detail.value = null;
    return;
  }
  expanded.value = name;
  detail.value = null;
  detailLoading.value = true;
  try {
    const value = await runToolsApi.detail(props.runId, name);
    if (current === detailVersion && expanded.value === name)
      detail.value = value;
  } catch (e) {
    if (current === detailVersion && expanded.value === name)
      error.value = e instanceof Error ? e.message : "工具契约读取失败";
  } finally {
    if (current === detailVersion) detailLoading.value = false;
  }
}

watch(
  () => props.runId,
  () => {
    detailVersion++;
    detailLoading.value = false;
    directory.value = null;
    error.value = "";
    query.value = "";
    source.value = "";
    status.value = "all";
    expanded.value = null;
    detail.value = null;
    offset.value = 0;
    void load();
  },
  { immediate: true },
);
watch(() => props.refreshKey, load);
onUnmounted(() => {
  detailVersion++;
  version += 1;
});
</script>

<template>
  <section class="run-tools" aria-label="本 Run 工具目录">
    <header class="run-tools-head">
      <h3>工具目录（Run 冻结事实）</h3>
      <span v-if="directory?.catalog_version" class="catalog-version"
        >版本 {{ directory.catalog_version.slice(0, 12) }}</span
      >
      <span v-if="directory" class="loaded-count"
        >已加载 {{ directory.loaded_count }}</span
      >
    </header>
    <p v-if="error" class="run-tools-error" role="alert">{{ error }}</p>
    <p v-if="loading" class="run-tools-loading">读取中…</p>
    <p class="run-tools-note">
      展示与发现不等于授权：执行仍由策略引擎逐次判定，这里没有执行入口。
    </p>
    <template v-if="directory">
      <p v-for="note in directory.notes" :key="note" class="run-tools-note">
        {{ note }}
      </p>
      <p v-if="directory.stale_receipts > 0" class="run-tools-note">
        {{ directory.stale_receipts }}
        条加载事实与冻结目录版本不一致，已按原样保留，不套用。
      </p>
      <div
        v-if="directory.catalog_frozen && directory.catalog_total > 0"
        class="run-tools-filters"
      >
        <input
          v-model="query"
          class="run-tools-input"
          placeholder="搜索名称或描述"
          aria-label="搜索工具"
          @input="requery"
        />
        <input
          v-model="source"
          class="run-tools-input narrow"
          placeholder="来源，如 mcp:demo"
          aria-label="按来源筛选"
          @input="requery"
        />
        <select
          v-model="status"
          class="run-tools-input narrow"
          aria-label="按状态筛选"
          @change="requery"
        >
          <option value="all">全部</option>
          <option value="loaded">已加载</option>
          <option value="unloaded">未加载</option>
          <option value="failed">有失败记录</option>
        </select>
      </div>
      <ul class="run-tools-list">
        <li v-for="item in directory.items" :key="item.name">
          <button
            type="button"
            class="run-tool-row"
            :aria-expanded="expanded === item.name"
            @click="toggle(item.name)"
          >
            <span class="run-tool-name">{{ item.name }}</span>
            <span class="run-tool-source">{{ item.source }}</span>
            <span
              class="run-tool-badge"
              :class="
                item.loaded
                  ? 'is-loaded'
                  : item.provided
                    ? 'is-provided'
                    : 'is-idle'
              "
              >{{
                item.loaded
                  ? "已加载"
                  : item.provided
                    ? "常驻"
                    : item.status === "available"
                      ? "未加载"
                      : item.status
              }}</span
            >
            <span
              class="run-tool-badge"
              :class="item.effect === 'readonly' ? 'is-readonly' : 'is-effect'"
              >{{ item.effect === "readonly" ? "只读" : "有副作用" }}</span
            >
          </button>
          <div v-if="expanded === item.name" class="run-tool-detail">
            <p v-if="detailLoading" class="run-tools-loading">契约读取中…</p>
            <template v-if="detail">
              <p class="run-tool-desc">
                {{ detail.description || "（无描述）" }}
              </p>
              <p class="run-tool-meta">
                环境 {{ detail.environment }} · schema版本
                {{ detail.version.slice(0, 12) }}
                <template v-if="detail.schema_status === 'oversize'">
                  · 过大未内联（{{ detail.schema_chars }} 字符，见冻结事实）
                </template>
                <template v-if="detail.loaded && detail.loaded_seq !== null">
                  · 第 {{ detail.loaded_seq }} 项事实加载
                </template>
              </p>
              <p class="run-tool-permission">
                展示与发现不等于授权：执行仍由策略引擎逐次判定，这里没有执行入口。
              </p>
              <pre v-if="detail.input_schema" class="run-tool-schema">{{
                JSON.stringify(detail.input_schema, null, 2)
              }}</pre>
            </template>
          </div>
        </li>
      </ul>
      <p
        v-if="directory.total === 0 && directory.catalog_frozen"
        class="run-tools-note"
      >
        没有匹配条目（目录共 {{ directory.catalog_total }} 项）。
      </p>
      <div
        v-if="directory.total > directory.items.length || offset > 0"
        class="run-tools-pager"
      >
        <button
          type="button"
          :disabled="offset === 0"
          @click="
            offset = Math.max(0, offset - PAGE_SIZE);
            void load();
          "
        >
          上一页
        </button>
        <span
          >第 {{ offset + 1 }}-{{ offset + directory.items.length }} /
          {{ directory.total }} 条</span
        >
        <button
          type="button"
          :disabled="offset + PAGE_SIZE >= directory.total"
          @click="
            offset += PAGE_SIZE;
            void load();
          "
        >
          下一页
        </button>
      </div>
      <ul v-if="directory.failed_loads.length" class="run-tools-failures">
        <li v-for="failure in directory.failed_loads" :key="failure.seq">
          加载失败（第 {{ failure.seq }} 项事实）：{{
            failure.tool || "未知工具"
          }}
          ·
          {{ failure.code || "unknown" }}
          <span v-if="failure.reason">— {{ failure.reason }}</span>
        </li>
      </ul>
    </template>
  </section>
</template>

<style scoped>
.run-tools {
  border: 1px solid var(--line, #ddd);
  border-radius: 8px;
  padding: 0.75rem;
  margin: 0.5rem 0;
  font-size: 0.85rem;
}
.run-tools-head {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  flex-wrap: wrap;
}
.run-tools-head h3 {
  margin: 0;
  font-size: 0.9rem;
}
.catalog-version,
.loaded-count {
  color: var(--muted, #666);
}
.run-tools-error {
  color: var(--danger, #b00020);
}
.run-tools-note {
  color: var(--muted, #666);
  margin: 0.25rem 0;
}
.run-tools-filters {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
  margin: 0.5rem 0;
}
.run-tools-input {
  flex: 1;
  min-width: 8rem;
}
.run-tools-input.narrow {
  flex: 0 1 10rem;
}
.run-tools-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.run-tool-row {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  width: 100%;
  background: none;
  border: none;
  border-top: 1px solid var(--line, #eee);
  padding: 0.35rem 0;
  cursor: pointer;
  text-align: left;
  font: inherit;
}
.run-tool-name {
  font-family: monospace;
  overflow-wrap: anywhere;
}
.run-tool-source {
  color: var(--muted, #666);
  flex: 1;
}
.run-tool-badge {
  border-radius: 999px;
  padding: 0 0.45rem;
  font-size: 0.72rem;
  border: 1px solid currentColor;
}
.is-loaded {
  color: var(--ok, #1b7f4d);
}
.is-provided {
  color: var(--accent, #3556c8);
}
.is-idle {
  color: var(--muted, #666);
}
.is-readonly {
  color: var(--ok, #1b7f4d);
}
.is-effect {
  color: var(--warn, #9a6700);
}
.run-tool-detail {
  padding: 0.25rem 0 0.5rem 0.5rem;
  border-top: 1px dashed var(--line, #eee);
}
.run-tool-desc {
  margin: 0.25rem 0;
}
.run-tool-meta,
.run-tool-permission {
  color: var(--muted, #666);
  margin: 0.15rem 0;
}
.run-tool-schema {
  background: var(--surface-2, #f6f6f6);
  border-radius: 6px;
  padding: 0.5rem;
  overflow-x: auto;
  max-height: 16rem;
  font-size: 0.75rem;
}
.run-tools-pager {
  display: flex;
  gap: 0.6rem;
  align-items: center;
  margin-top: 0.4rem;
}
.run-tools-failures {
  margin: 0.4rem 0 0;
  padding-left: 1.1rem;
  color: var(--danger, #b00020);
}
</style>
