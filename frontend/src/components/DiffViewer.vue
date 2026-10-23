<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from "vue";
const props = defineProps<{ patch: string }>();
interface DiffLine {
  kind: "added" | "removed" | "context" | "hunk" | "note";
  text: string;
  old: number | null;
  next: number | null;
}
interface DiffFile {
  path: string;
  kind: "新增文件" | "修改文件" | "删除文件";
  unavailable: boolean;
  lines: DiffLine[];
}
const instanceId = useId();
const renderLimit = 2_000_000;
const overLimit = computed(() => {
  if (props.patch.length > renderLimit) return true;
  let offset = -1;
  for (let count = 0; count <= 20_000; count++) {
    offset = props.patch.indexOf("\n", offset + 1);
    if (offset === -1) return false;
  }
  return true;
});
const files = computed(() => {
  const result: DiffFile[] = [];
  if (overLimit.value) return result;
  let file: DiffFile | undefined;
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  for (const line of props.patch.split("\n")) {
    if (line.startsWith("diff --git ")) {
      file = {
        path: line.slice(11),
        kind: "修改文件",
        unavailable: false,
        lines: [],
      };
      result.push(file);
      inHunk = false;
    } else if (line.startsWith("--- ") && !inHunk) {
      if (!file || file.lines.length) {
        file = { path: "", kind: "修改文件", unavailable: false, lines: [] };
        result.push(file);
      }
      const path = line.slice(4).split("\t")[0]!;
      file.path = path.replace(/^a\//, "");
      if (path === "/dev/null") file.kind = "新增文件";
    } else if (line.startsWith("+++ ") && !inHunk && file) {
      const path = line.slice(4).split("\t")[0]!;
      if (path === "/dev/null") file.kind = "删除文件";
      else file.path = path.replace(/^b\//, "");
    } else if (file && line.startsWith("@@ ")) {
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
        inHunk = true;
        file.lines.push({ kind: "hunk", text: line, old: null, next: null });
      }
    } else if (file && inHunk && /^[ +\-\\]/.test(line)) {
      const prefix = line[0];
      file.lines.push({
        kind:
          prefix === "+"
            ? "added"
            : prefix === "-"
              ? "removed"
              : prefix === " "
                ? "context"
                : "note",
        text: prefix === "\\" ? line : line.slice(1),
        old: prefix === " " || prefix === "-" ? oldLine++ : null,
        next: prefix === " " || prefix === "+" ? newLine++ : null,
      });
    } else if (file && /^(Binary files |GIT binary patch)/.test(line)) {
      file.unavailable = true;
    } else if (file && line.startsWith("new file mode ")) {
      file.kind = "新增文件";
    } else if (file && line.startsWith("deleted file mode ")) {
      file.kind = "删除文件";
    }
  }
  return result;
});
const visibleFiles = ref(50);
const opened = ref<Record<number, boolean>>({ 0: true });
const limits = ref<Record<number, number>>({});
watch(
  () => props.patch,
  () => {
    visibleFiles.value = 50;
    opened.value = { 0: true };
    limits.value = {};
  },
);
function toggle(index: number, event: Event) {
  opened.value[index] = (event.currentTarget as HTMLDetailsElement).open;
}
async function showFile(index: number) {
  opened.value[index] = true;
  await nextTick();
  document.getElementById(`diff-file-${instanceId}-${index}`)?.scrollIntoView();
}
</script>
<template>
  <section class="diff-viewer" aria-label="代码改动对比">
    <p v-if="!patch">无内容差异。</p>
    <p v-else-if="overLimit">
      差异超过页面展示上限（2,000,000 字符或 20,000
      行），请在工作副本中检查文件。
    </p>
    <template v-else-if="files.length">
      <p class="muted">{{ files.length }} 个文件；旧行号 → 新行号</p>
      <ul class="diff-files" aria-label="改动文件列表">
        <li v-for="(file, index) in files.slice(0, visibleFiles)" :key="index">
          <button type="button" @click="showFile(index)">
            {{ file.path }}
          </button>
          · {{ file.kind }}
        </li>
      </ul>
      <details
        v-for="(file, index) in files.slice(0, visibleFiles)"
        :id="`diff-file-${instanceId}-${index}`"
        :key="index"
        :open="opened[index] || false"
        class="diff-file"
        @toggle="toggle(index, $event)"
      >
        <summary>{{ file.path }} · {{ file.kind }}</summary>
        <template v-if="opened[index]">
          <p v-if="file.unavailable">此文件为二进制内容，无法展示文本差异。</p>
          <p v-else-if="!file.lines.length">
            无文本行差异（可能仅文件属性变化）。
          </p>
          <div v-else class="diff-scroll" tabindex="0">
            <table aria-label="逐行差异">
              <tbody>
                <tr
                  v-for="(line, row) in file.lines.slice(
                    0,
                    limits[index] || 200,
                  )"
                  :key="row"
                  :class="`diff-${line.kind}`"
                >
                  <td class="line-number">{{ line.old }}</td>
                  <td class="line-number">{{ line.next }}</td>
                  <td class="diff-sign" aria-hidden="true">
                    {{
                      line.kind === "added"
                        ? "+"
                        : line.kind === "removed"
                          ? "−"
                          : ""
                    }}
                  </td>
                  <td class="diff-text">
                    <code>{{ line.text }}</code>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <button
            v-if="file.lines.length > (limits[index] || 200)"
            @click="limits[index] = (limits[index] || 200) + 200"
          >
            再显示 200 行（共 {{ file.lines.length }} 行）
          </button>
        </template>
      </details>
      <button v-if="files.length > visibleFiles" @click="visibleFiles += 50">
        再显示 50 个文件（共 {{ files.length }} 个）
      </button>
    </template>
    <p v-else>无法解析此补丁的逐行差异，请检查补丁来源与格式。</p>
  </section>
</template>
<style scoped>
.diff-viewer {
  min-width: 0;
}
.diff-files {
  overflow-wrap: anywhere;
}
.diff-file {
  margin: 0.6rem 0;
}
.diff-file summary {
  overflow-wrap: anywhere;
}
.diff-scroll {
  overflow: auto;
  max-height: 32rem;
  border: 1px solid var(--border, #8884);
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 0.8rem;
}
td {
  padding: 0 0.4rem;
  vertical-align: top;
}
.line-number {
  width: 3rem;
  text-align: right;
  user-select: none;
  opacity: 0.7;
}
.diff-sign {
  width: 1rem;
  user-select: none;
}
.diff-text {
  white-space: pre;
}
.diff-added {
  background: #22863a22;
  color: var(--diff-added-color, #176b2c);
}
.diff-removed {
  background: #cb243122;
  color: var(--diff-removed-color, #af2031);
}
.diff-hunk,
.diff-note {
  opacity: 0.7;
}
:global([data-theme="dark"]) .diff-added {
  color: #8de6a0;
}
:global([data-theme="dark"]) .diff-removed {
  color: #ff9aa5;
}
</style>
