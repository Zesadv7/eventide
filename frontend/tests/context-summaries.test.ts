import { mount, flushPromises } from "@vue/test-utils";
import { beforeEach, expect, it, vi } from "vitest";
import ContextSummaryPanel from "../src/components/ContextSummaryPanel.vue";
import SummaryVersionCard from "../src/components/SummaryVersionCard.vue";
import {
  contextSummariesApi,
  type SummaryDetail,
} from "../src/api/contextSummaries";
vi.mock("../src/api/contextSummaries", () => ({
  contextSummariesApi: { list: vi.fn(), detail: vi.fn(), message: vi.fn() },
}));
beforeEach(() => vi.clearAllMocks());
const detail = (id = 1): SummaryDetail => ({
  id,
  covered_seq: 20,
  source_digest: "digest",
  policy_version: 1,
  provider: "p",
  model: "m",
  summary_characters: 200,
  summary_hash: "hash",
  summary: "legacy",
  summary_truncated: false,
  structured: {
    goal: "goal",
    hard_constraints: [
      { text: "保留 API", src: [{ msg: 0, quote: "不要修改 API" }] },
    ],
    facts: [],
    checks: [
      {
        text: "pytest",
        status: "failed",
        src: [{ msg: 1, quote: "test failed" }],
      },
    ],
    failures: [],
    unverified: ["原因尚未确认"],
    next_steps: [],
  },
  source_verified: true,
  source_problem: null,
  input_verification: "matched",
  history_hash: "history",
  history_message_count: 2,
  citations_problem: null,
});
it("compares saved versions on demand and opens bounded original evidence", async () => {
  vi.mocked(contextSummariesApi.list).mockResolvedValue({
    session_id: "s",
    versions: [detail(2), detail(1)],
    next_before: null,
  });
  vi.mocked(contextSummariesApi.detail).mockImplementation(async (_s, id) =>
    detail(id),
  );
  vi.mocked(contextSummariesApi.message).mockResolvedValue({
    checkpoint_id: 2,
    source_digest: "digest",
    history_hash: "history",
    input_verification: "matched",
    message_index: 0,
    offset: 0,
    content: "<img src=x onerror=bad> 不要修改 API",
    total_characters: 100,
    next_offset: 30,
  });
  const wrapper = mount(ContextSummaryPanel, { props: { sessionId: "s" } });
  expect(contextSummariesApi.list).not.toHaveBeenCalled();
  wrapper.element.setAttribute("open", "");
  await wrapper.trigger("toggle");
  await flushPromises();
  expect(wrapper.findAllComponents(SummaryVersionCard)).toHaveLength(2);
  expect(wrapper.text()).toContain("[失败]");
  expect(wrapper.text()).toContain("原因尚未确认");
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "查看原始消息 0")!
    .trigger("click");
  await flushPromises();
  expect(contextSummariesApi.message).toHaveBeenCalledWith("s", 2, 0, 0);
  expect(wrapper.find("img").exists()).toBe(false);
  expect(wrapper.text()).toContain("<img src=x onerror=bad>");
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "下一段原始消息")!
    .trigger("click");
  expect(contextSummariesApi.message).toHaveBeenLastCalledWith("s", 2, 0, 30);
  wrapper.unmount();
});
it("late evidence from a previous version cannot replace a new version", async () => {
  vi.mocked(contextSummariesApi.detail).mockImplementation(async (_s, id) =>
    detail(id),
  );
  let resolve!: (value: any) => void;
  vi.mocked(contextSummariesApi.message).mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const wrapper = mount(SummaryVersionCard, {
    props: { sessionId: "s", checkpointId: 1 },
  });
  await flushPromises();
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "查看原始消息 0")!
    .trigger("click");
  await wrapper.setProps({ checkpointId: 2 });
  await flushPromises();
  resolve({ content: "stale evidence", next_offset: null });
  await flushPromises();
  expect(wrapper.text()).toContain("版本 2");
  expect(wrapper.text()).not.toContain("stale evidence");
  wrapper.unmount();
});
it("failed source verification and legacy records remain explicit", async () => {
  vi.mocked(contextSummariesApi.detail).mockResolvedValue({
    ...detail(),
    structured: null,
    source_verified: false,
    source_problem: "历史指纹不匹配",
    input_verification: "unavailable",
    summary_truncated: true,
  });
  const wrapper = mount(SummaryVersionCard, {
    props: { sessionId: "s", checkpointId: 1 },
  });
  await flushPromises();
  expect(wrapper.text()).toContain("历史指纹不匹配");
  expect(wrapper.text()).toContain("省略部分未展示");
  expect(wrapper.text()).not.toContain("重建输入与当时摘要请求的指纹一致");
  expect(wrapper.text()).toContain("legacy");
  wrapper.unmount();
});
it("switching sessions discards late version listings", async () => {
  let resolve!: (value: any) => void;
  vi.mocked(contextSummariesApi.list)
    .mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    )
    .mockResolvedValueOnce({
      session_id: "new",
      versions: [],
      next_before: null,
    });
  const wrapper = mount(ContextSummaryPanel, { props: { sessionId: "old" } });
  wrapper.element.setAttribute("open", "");
  await wrapper.trigger("toggle");
  await wrapper.setProps({ sessionId: "new" });
  await flushPromises();
  resolve({ session_id: "old", versions: [detail()], next_before: null });
  await flushPromises();
  expect(wrapper.text()).toContain("没有已保存摘要");
  expect(contextSummariesApi.detail).not.toHaveBeenCalled();
  wrapper.unmount();
});
