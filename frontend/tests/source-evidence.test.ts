import { expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import SourceEvidencePanel from "../src/components/SourceEvidencePanel.vue";
import ToolActivity from "../src/components/ToolActivity.vue";

it("shows read version and pagination without implying current freshness", () => {
  const wrapper = mount(SourceEvidencePanel, {
    props: {
      evidence: {
        kind: "file",
        path: "a.txt",
        status: "read",
        sha256: "a".repeat(64),
        total_lines: 20,
        start_line: 2,
        end_line: 4,
        start_character: 3,
        end_character: 10,
        next_token: "evtfile1:4:10:version",
        complete: false,
      },
      content: "historical page",
    },
  });
  expect(wrapper.text()).toContain("第 2–4 行");
  expect(wrapper.text()).toContain("查看下一页凭据");
  expect(wrapper.text()).toContain("文件后来可能发生变化");
  expect(wrapper.text()).not.toContain("本页已到文件末尾");
});

it("renders incomplete search source text without executing embedded HTML", () => {
  const wrapper = mount(SourceEvidencePanel, {
    props: {
      evidence: {
        kind: "search",
        mode: "regex",
        case_sensitive: true,
        context_lines: 1,
        scanned: 4,
        skipped: 2,
        unverified: 1,
        timed_out: true,
        output_capped: false,
        partial: true,
        next_offset: null,
        sources: [
          {
            path: "<img src=x onerror=alert(1)>",
            sha256: "b".repeat(64),
            lines: [
              { line: 8, match: true, text: "<script>alert(1)</script>" },
            ],
          },
        ],
      },
    },
  });
  expect(wrapper.find("script").exists()).toBe(false);
  expect(wrapper.find("img").exists()).toBe(false);
  expect(wrapper.text()).toContain("命中 8:");
  expect(wrapper.text()).toContain("结果不完整");
  expect(wrapper.text()).toContain("剩余文件尚未搜索");
});

it("shows stale reads as failures and old history as lacking structured evidence", () => {
  const wrapper = mount(SourceEvidencePanel, {
    props: {
      evidence: {
        kind: "file",
        path: "a.txt",
        status: "error",
      },
      content: "Error: File changed since the previous page",
    },
  });
  expect(wrapper.text()).toContain("本次读取未完成");
  expect(wrapper.text()).not.toContain("读取时的 SHA-256");
  const history = mount(ToolActivity, {
    props: { tools: { c: { name: "read_file", content: "old text" } } },
  });
  expect(history.text()).toContain("没有结构化源码证据");
});
