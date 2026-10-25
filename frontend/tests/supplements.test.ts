import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import {
  interactionsApi,
  type SupplementProjection,
} from "../src/api/interactions";
import RunSupplementPanel from "../src/components/RunSupplementPanel.vue";

const record = (extra: Record<string, unknown> = {}) => ({
  supplement_id: "sup_one",
  run_id: "r1",
  session_id: "s1",
  body: "补充正文",
  body_version: 1,
  status: "pending" as const,
  consumed_step: null,
  ...extra,
});
const projection = (
  extra: Partial<SupplementProjection> = {},
): SupplementProjection => ({
  run_id: "r1",
  supplements: [record()],
  pending: 1,
  ...extra,
});
beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
});

function mountPanel(props: Record<string, unknown> = {}) {
  return mount(RunSupplementPanel, {
    props: { runId: "r1", runStatus: "running", ...props },
  });
}

it("shows pending and consumed supplements without claiming auto resume", async () => {
  vi.spyOn(interactionsApi, "list").mockResolvedValue(
    projection({
      supplements: [
        record(),
        record({
          supplement_id: "sup_two",
          status: "consumed",
          consumed_step: 2,
          body: "第二条",
        }),
      ],
    }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  expect(wrapper.text()).toContain("1 条待消费");
  expect(wrapper.text()).toContain("等待下一模型边界");
  expect(wrapper.text()).toContain("已消费一次");
  expect(wrapper.text()).toContain("第 2 步边界消费");
  expect(wrapper.text()).toContain("已执行的工具不会倒带");
  wrapper.unmount();
});

it("sends a supplement, reloads, and reports late receipts never resume", async () => {
  const list = vi
    .spyOn(interactionsApi, "list")
    .mockResolvedValueOnce(projection({ supplements: [], pending: 0 }))
    .mockResolvedValue(projection());
  const add = vi.spyOn(interactionsApi, "add").mockResolvedValue({
    supplement_id: "sup_one",
    run_id: "r1",
    status: "late",
    consumed: false,
  });
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find("textarea").setValue("运行结束后才想起的补充");
  await wrapper.find("button.primary").trigger("click");
  await flushPromises();
  expect(add).toHaveBeenCalledWith("r1", "运行结束后才想起的补充");
  expect(list).toHaveBeenCalledTimes(2);
  expect(wrapper.text()).toContain("不会自动恢复或重开任何运行");
  expect(wrapper.find("textarea").element.value).toBe("");
  wrapper.unmount();
});

it("reports active consumption notice and send failures distinctly", async () => {
  vi.spyOn(interactionsApi, "list").mockResolvedValue(
    projection({ supplements: [], pending: 0 }),
  );
  vi.spyOn(interactionsApi, "add").mockRejectedValue(
    new Error("结果未知，请保留原请求"),
  );
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find("textarea").setValue("补充信息");
  await wrapper.find("button.primary").trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("结果未知");
  expect(wrapper.text()).not.toContain("将在下一个模型边界加入");
  wrapper.unmount();

  vi.spyOn(interactionsApi, "add").mockResolvedValue({
    supplement_id: "sup_two",
    run_id: "r1",
    status: "pending",
    consumed: false,
  });
  const second = mountPanel();
  await flushPromises();
  await second.find("textarea").setValue("补充信息");
  await second.find("button.primary").trigger("click");
  await flushPromises();
  expect(second.text()).toContain("将在下一个模型边界加入当前运行");
  second.unmount();
});

it("disables send for empty drafts only", async () => {
  vi.spyOn(interactionsApi, "list").mockResolvedValue(
    projection({ supplements: [], pending: 0 }),
  );
  const add = vi.spyOn(interactionsApi, "add");
  const wrapper = mountPanel();
  await flushPromises();
  const send = wrapper.find("button.primary");
  expect(send.attributes("disabled")).toBeDefined();
  await wrapper.find("textarea").setValue("   ");
  expect(send.attributes("disabled")).toBeDefined();
  await wrapper.find("textarea").setValue("真实补充");
  expect(send.attributes("disabled")).toBeUndefined();
  expect(add).not.toHaveBeenCalled();
  wrapper.unmount();
});

it("ignores an old run send response after switching runs", async () => {
  vi.spyOn(interactionsApi, "list").mockImplementation(async (id) =>
    projection({ run_id: id, supplements: [], pending: 0 }),
  );
  let finish!: (value: Awaited<ReturnType<typeof interactionsApi.add>>) => void;
  vi.spyOn(interactionsApi, "add").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find("textarea").setValue("old run instruction");
  await wrapper.find("button.primary").trigger("click");
  await wrapper.setProps({ runId: "r2" });
  await flushPromises();
  await wrapper.find("textarea").setValue("new run draft");
  finish({
    supplement_id: "sup_old",
    run_id: "r1",
    status: "late",
    consumed: false,
  });
  await flushPromises();
  expect(wrapper.find("textarea").element.value).toBe("new run draft");
  expect(wrapper.text()).not.toContain("该运行已结束");
  wrapper.unmount();
});
