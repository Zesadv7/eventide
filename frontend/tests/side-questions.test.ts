import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { api } from "../src/api/client";
import {
  sideQuestionsApi,
  type SideQuestionView,
} from "../src/api/sideQuestions";
import SideQuestionPanel from "../src/components/SideQuestionPanel.vue";

const view = (extra: Partial<SideQuestionView> = {}): SideQuestionView => ({
  side_question_id: "sq_one",
  side_run_id: "run_one",
  side_session_id: "ss1",
  source_run_id: "r1",
  source_session_id: "s1",
  question: "code.py 里是什么？",
  source_high_water: 12,
  snapshot_id: "abcdef1234567890",
  status: "running",
  side_run_status: "running",
  answer: "",
  answer_fingerprint: null,
  returned: false,
  supplement: null,
  source_context: {
    user_prompt: "修好解析器",
    tool_activity: "2 tool calls, recent: read_file",
    evidence_events: 9,
  },
  ...extra,
});
const projection = (items: SideQuestionView[]) => ({
  run_id: "r1",
  side_questions: items,
});

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mountPanel(props: Record<string, unknown> = {}) {
  return mount(SideQuestionPanel, {
    props: { runId: "r1", sessionId: "s1", runStatus: "running", ...props },
  });
}

it("asks a side question without touching the main run and reloads", async () => {
  const list = vi
    .spyOn(sideQuestionsApi, "list")
    .mockResolvedValueOnce(projection([]))
    .mockResolvedValue(projection([view()]));
  const ask = vi.spyOn(sideQuestionsApi, "ask").mockResolvedValue({
    side_question_id: "sq_one",
    side_run_id: "run_one",
    side_session_id: "ss1",
    source_run_id: "r1",
    source_session_id: "s1",
    question: "code.py 里是什么？",
    source_high_water: 12,
    snapshot_id: "abcdef1234567890",
    status: "running",
  });
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find("textarea").setValue("code.py 里是什么？");
  await wrapper.find("button.primary").trigger("click");
  await flushPromises();
  expect(ask).toHaveBeenCalledWith("s1", "code.py 里是什么？", "r1");
  expect(wrapper.text()).toContain("主任务不受影响");
  expect(wrapper.text()).toContain("调查中");
  expect(wrapper.text()).toContain("快照 abcdef123456");
  expect(wrapper.text()).toContain("共 9");
  expect(list).toHaveBeenCalledTimes(2);
  wrapper.unmount();
});

it("returns the seen answer once, marks truncation and late distinctly", async () => {
  const done = view({
    side_run_status: "completed",
    status: "completed",
    answer: "value = 1",
    answer_fingerprint: "f".repeat(64),
  });
  const returnedView = view({
    side_run_status: "completed",
    status: "completed",
    answer: "value = 1",
    answer_fingerprint: "f".repeat(64),
    returned: true,
    supplement: { supplement_id: "sup_one", status: "late", consumed: false },
  });
  vi.spyOn(sideQuestionsApi, "list")
    .mockResolvedValueOnce(projection([done]))
    .mockResolvedValue(projection([returnedView]));
  const ret = vi.spyOn(sideQuestionsApi, "return").mockResolvedValue({
    side_question_id: "sq_one",
    side_run_id: "run_one",
    source_run_id: "r1",
    supplement_id: "sup_one",
    status: "late",
    returned: true,
    already_returned: false,
    answer_fingerprint: "f".repeat(64),
    body_truncated: true,
    answer_length: 9000,
  });
  const wrapper = mountPanel({ runStatus: "completed" });
  await flushPromises();
  const buttons = wrapper.findAll("button");
  const returnButton = buttons.find((b) => b.text().includes("回传答案"));
  expect(returnButton).toBeTruthy();
  await returnButton!.trigger("click");
  await flushPromises();
  expect(ret).toHaveBeenCalledWith("sq_one", "value = 1");
  expect(wrapper.text()).toContain("不会自动续跑");
  expect(wrapper.text()).toContain("已回传（迟到）");
  // The return button is gone after returning: a second click cannot re-inject.
  expect(
    wrapper.findAll("button").some((b) => b.text().includes("回传答案")),
  ).toBe(false);
  wrapper.unmount();
});

it("offers stop only while the side run is active", async () => {
  const cancel = vi.spyOn(api, "cancel").mockResolvedValue({});
  vi.spyOn(sideQuestionsApi, "list").mockResolvedValue(
    projection([
      view({
        side_run_status: "completed",
        status: "completed",
        answer: "x",
      }),
      view({ side_question_id: "sq_two", side_run_id: "run_two" }),
    ]),
  );
  const wrapper = mountPanel();
  await flushPromises();
  const stop = wrapper
    .findAll("button")
    .find((b) => b.text().includes("停止旁枝"));
  expect(stop).toBeTruthy();
  await stop!.trigger("click");
  await flushPromises();
  expect(cancel).toHaveBeenCalledWith("run_two");
  wrapper.unmount();
});

it("late creation cannot clear a different run's draft or show its receipt", async () => {
  vi.spyOn(sideQuestionsApi, "list").mockResolvedValue(projection([]));
  let finish!: (value: any) => void;
  vi.spyOn(sideQuestionsApi, "ask").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find("textarea").setValue("old question");
  await wrapper.find("button.primary").trigger("click");
  await wrapper.setProps({ runId: "r2", sessionId: "s2" });
  await wrapper.find("textarea").setValue("new draft");
  finish({ side_question_id: "old" });
  await flushPromises();
  expect((wrapper.find("textarea").element as HTMLTextAreaElement).value).toBe(
    "new draft",
  );
  expect(wrapper.text()).not.toContain("旁枝调查已开始");
  wrapper.unmount();
});

it("late return errors cannot leak into a different run", async () => {
  vi.spyOn(sideQuestionsApi, "list").mockResolvedValue(
    projection([view({ side_run_status: "completed", answer: "answer" })]),
  );
  let fail!: (reason: Error) => void;
  vi.spyOn(sideQuestionsApi, "return").mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        fail = reject;
      }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper
    .findAll("button")
    .find((b) => b.text().includes("回传答案"))!
    .trigger("click");
  await wrapper.setProps({ runId: "r2", sessionId: "s2" });
  fail(new Error("old return failure"));
  await flushPromises();
  expect(wrapper.text()).not.toContain("old return failure");
  wrapper.unmount();
});

it("unknown create and return responses retain their request identity without storing text", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(Response.json({ side_question_id: "sq_one" }))
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(Response.json({ returned: true }));
  vi.stubGlobal("fetch", fetch);
  await expect(
    sideQuestionsApi.ask("s1", "private question", "r1"),
  ).rejects.toThrow("offline");
  expect(JSON.stringify({ ...sessionStorage })).not.toContain(
    "private question",
  );
  await sideQuestionsApi.ask("s1", "private question", "r1");
  expect(fetch.mock.calls[1]![1].headers["Idempotency-Key"]).toBe(
    fetch.mock.calls[0]![1].headers["Idempotency-Key"],
  );
  await expect(
    sideQuestionsApi.return("sq_one", "private answer"),
  ).rejects.toThrow("offline");
  expect(JSON.stringify({ ...sessionStorage })).not.toContain("private answer");
  await sideQuestionsApi.return("sq_one", "private answer");
  expect(fetch.mock.calls[3]![1].headers["Idempotency-Key"]).toBe(
    fetch.mock.calls[2]![1].headers["Idempotency-Key"],
  );
});
