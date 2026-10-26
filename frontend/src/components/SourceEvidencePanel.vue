<script setup lang="ts">
import type { SourceEvidence } from "../api/sourceEvidence";
defineProps<{ evidence: SourceEvidence; content?: string }>();
</script>
<template>
  <section class="source-evidence" aria-label="源码读取证据">
    <template v-if="evidence.kind === 'file'">
      <h4>{{ evidence.path }}</h4>
      <template v-if="evidence.status === 'read'">
        <p v-if="evidence.start_line !== undefined">
          本页：第 {{ evidence.start_line }}–{{ evidence.end_line }} 行，文件共
          {{ evidence.total_lines }} 行。起始行字符偏移
          {{ evidence.start_character }}， 末行结束偏移
          {{ evidence.end_character }}（字符从 0 计，结束位置不含在内）。
        </p>
        <p v-else-if="evidence.total_lines === 0">这是一个空文件。</p>
        <p v-else>本次未返回正文行，请检查读取范围。</p>
        <p>
          读取时的 SHA-256：<code>{{ evidence.sha256 }}</code>
        </p>
        <p v-if="evidence.complete">本页已到文件末尾。</p>
        <details v-if="evidence.next_token">
          <summary>还有内容：查看下一页凭据</summary>
          <p>这个凭据只适用于同一工作副本和文件版本；文件变化后需重新读取。</p>
          <code>{{ evidence.next_token }}</code>
        </details>
      </template>
      <p v-else role="status">
        本次读取未完成；下面是具体原因，没有把旧页当作新证据。
      </p>
      <pre v-if="content">{{ content }}</pre>
    </template>
    <template v-else>
      <h4>文本搜索来源</h4>
      <p>
        {{ evidence.mode === "regex" ? "正则" : "字面" }}搜索 ·
        {{ evidence.case_sensitive ? "区分大小写" : "忽略大小写" }} · 上下文
        {{ evidence.context_lines }} 行
      </p>
      <p>
        扫描 {{ evidence.scanned }} 个文件，跳过
        {{ evidence.skipped }} 个，版本核对未通过 {{ evidence.unverified }} 个。
      </p>
      <p
        v-if="evidence.partial || evidence.next_offset !== null"
        class="evidence-warning"
      >
        结果不完整。没有匹配不代表整个项目都不存在该内容。
      </p>
      <p v-if="evidence.timed_out">搜索到达时间上限，剩余文件尚未搜索。</p>
      <p v-if="evidence.output_capped">
        搜索输出到达上限，可能漏掉后面的匹配。
      </p>
      <p v-if="evidence.next_offset !== null">
        下一页匹配偏移：{{ evidence.next_offset }}
      </p>
      <details v-for="source in evidence.sources" :key="source.path" open>
        <summary>{{ source.path }}</summary>
        <p>
          搜索时的 SHA-256：<code>{{ source.sha256 }}</code>
        </p>
        <pre><span v-for="line in source.lines" :key="line.line" :class="{ 'matched-line': line.match }">{{ line.match ? '命中' : '上下文' }} {{ line.line }}: {{ line.text }}{{ '\n' }}</span></pre>
      </details>
      <p v-if="!evidence.sources.length">已搜索范围内没有可展示的匹配。</p>
    </template>
    <p class="muted">
      以上是工具调用当时的证据；文件后来可能发生变化，不能据此宣称当前版本仍一致。
    </p>
  </section>
</template>
<style scoped>
.source-evidence {
  overflow-wrap: anywhere;
}
.source-evidence pre {
  white-space: pre-wrap;
  max-height: 24rem;
  overflow: auto;
}
.matched-line {
  font-weight: 650;
}
.evidence-warning {
  font-weight: 600;
}
</style>
