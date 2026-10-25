import { afterEach, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import ConnectionNotice from "../src/components/ConnectionNotice.vue";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
it("shows retry countdown, offline recovery and clears its clock on unmount", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  const interval = vi.spyOn(globalThis, "setInterval");
  const clear = vi.spyOn(globalThis, "clearInterval");
  const wrapper = mount(ConnectionNotice, {
    props: { status: { state: "retrying", attempt: 2, nextRetryAt: 3000 } },
  });
  expect(wrapper.get('[role="status"]').text()).toContain("2 秒后第 2 次重连");
  await vi.advanceTimersByTimeAsync(1000);
  expect(wrapper.text()).toContain("1 秒后第 2 次重连");
  await wrapper.setProps({
    status: { state: "offline", attempt: 2, nextRetryAt: null },
  });
  expect(wrapper.text()).toContain("联网后会自动接回任务进度");
  expect(wrapper.text()).toContain("不会因页面断线而重新提交");
  wrapper.unmount();
  expect(clear).toHaveBeenCalledWith(interval.mock.results[0]!.value);
});
