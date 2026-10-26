import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import RunQuestionPanel from "../src/components/RunQuestionPanel.vue";
import {
  interactionsApi,
  type QuestionProjection,
  type QuestionAnswerReceipt,
} from "../src/api/interactions";
const question = (run = "r1"): QuestionProjection => ({
  run_id: run,
  waiting: 1,
  questions: [
    {
      question_id: `${run}-question`,
      run_id: run,
      revision: 1,
      status: "waiting",
      questions: [
        {
          text: "<img src=x onerror=alert(1)>选择语言？",
          choices: ["中文", "英文"],
          allow_free_text: true,
        },
      ],
    },
  ],
});
afterEach(() => vi.restoreAllMocks());
describe("structured questions", () => {
  it("requires an explicit answer and returns choice plus free text to the exact question", async () => {
    vi.spyOn(interactionsApi, "listQuestions").mockResolvedValue(question());
    const send = vi.spyOn(interactionsApi, "answerQuestion").mockResolvedValue({
      question_id: "r1-question",
      run_id: "r1",
      status: "delivered",
      duplicate: false,
    });
    const wrapper = mount(RunQuestionPanel, { props: { runId: "r1" } });
    await flushPromises();
    const button = () =>
      wrapper.findAll("button").find((b) => b.text() === "提交回答")!;
    expect(button().attributes()).toHaveProperty("disabled");
    expect(wrapper.find("img").exists()).toBe(false);
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "中文")!
      .trigger("click");
    await wrapper.get("textarea").setValue("保留英文标识符");
    await button().trigger("click");
    await flushPromises();
    expect(send).toHaveBeenCalledWith(
      "r1",
      "r1-question",
      [{ choice: "中文", text: "保留英文标识符" }],
      1,
    );
    expect(wrapper.text()).toContain("已回答");
    wrapper.unmount();
  });

  it("keeps the answer after an unknown failure so the same content can be retried", async () => {
    vi.spyOn(interactionsApi, "listQuestions").mockResolvedValue(question());
    const send = vi
      .spyOn(interactionsApi, "answerQuestion")
      .mockRejectedValueOnce(new Error("连接中断"))
      .mockResolvedValue({
        question_id: "r1-question",
        run_id: "r1",
        status: "delivered",
        duplicate: true,
      });
    const wrapper = mount(RunQuestionPanel, { props: { runId: "r1" } });
    await flushPromises();
    await wrapper.get("textarea").setValue("中文");
    const button = () =>
      wrapper.findAll("button").find((b) => b.text() === "提交回答")!;
    await button().trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("连接中断");
    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe(
      "中文",
    );
    await button().trigger("click");
    await flushPromises();
    expect(send.mock.calls[0]).toEqual(send.mock.calls[1]);
    wrapper.unmount();
  });

  it("isolates a late answer response from another run's draft", async () => {
    vi.spyOn(interactionsApi, "listQuestions").mockImplementation(async (run) =>
      question(run),
    );
    let finish!: (value: QuestionAnswerReceipt) => void;
    vi.spyOn(interactionsApi, "answerQuestion").mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const wrapper = mount(RunQuestionPanel, { props: { runId: "r1" } });
    await flushPromises();
    await wrapper.get("textarea").setValue("旧答案");
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "提交回答")!
      .trigger("click");
    await wrapper.setProps({ runId: "r2" });
    await flushPromises();
    await wrapper.get("textarea").setValue("新答案");
    finish({
      question_id: "r1-question",
      run_id: "r1",
      status: "delivered",
      duplicate: false,
    });
    await flushPromises();
    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe(
      "新答案",
    );
    expect(wrapper.text()).not.toContain("已回答；");
    wrapper.unmount();
  });

  it("never replaces the current run with a late question fetch", async () => {
    let finish!: (value: QuestionProjection) => void;
    vi.spyOn(interactionsApi, "listQuestions").mockImplementation((run) =>
      run === "r1"
        ? new Promise((resolve) => {
            finish = resolve;
          })
        : Promise.resolve(question(run)),
    );
    const wrapper = mount(RunQuestionPanel, { props: { runId: "r1" } });
    await wrapper.setProps({ runId: "r2" });
    await flushPromises();
    finish(question("r1"));
    await flushPromises();
    expect(wrapper.text()).toContain("r2-question");
    expect(wrapper.text()).not.toContain("r1-question");
    wrapper.unmount();
  });
});
