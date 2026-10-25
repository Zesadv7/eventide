import { afterEach, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import ModelRetryNotice from "../src/components/ModelRetryNotice.vue";

afterEach(() => vi.useRealTimers());
it("uses the server retry deadline and keeps the active wait visible after it", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  const wrapper = mount(ModelRetryNotice, {
    props: {
      retry: {
        attempt: 1,
        next_attempt: 2,
        category: "rate_limited",
        retry_at: 3,
      },
    },
  });
  expect(wrapper.get('[role="status"]').text()).toContain("模型服务暂时限流");
  expect(wrapper.text()).toContain("2 秒后自动进行第 2 次尝试");
  await vi.advanceTimersByTimeAsync(3000);
  expect(wrapper.text()).toContain("正在等待第 2 次尝试");
  expect(wrapper.text()).toContain("仍可停止");
  wrapper.unmount();
  expect(vi.getTimerCount()).toBe(0);
});
