import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import UsageView from "../src/views/UsageView.vue";
import { budgetsApi } from "../src/api/budgets";
import { costs } from "./fixtures";

beforeEach(() => {
  setActivePinia(createPinia());
});

it("loads exact delivery scope after navigation and refresh without reaccumulating", async () => {
  const fetch = vi.spyOn(budgetsApi, "costs").mockResolvedValue(costs);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/usage", component: UsageView }],
  });
  await router.push("/usage?delivery=delivery-one");
  await router.isReady();
  const wrapper = mount(UsageView, {
    global: {
      plugins: [router],
      stubs: { BudgetPanel: true, PriceEditor: true },
    },
  });
  await flushPromises();
  expect(fetch).toHaveBeenLastCalledWith({
    kind: "delivery",
    identity: "delivery-one",
  });
  expect(wrapper.text()).toContain("指定交付全部轮次");
  expect(wrapper.text()).toContain("关闭或重启交付不会清零");
  await router.push("/usage?delivery=delivery-two");
  await flushPromises();
  expect(fetch).toHaveBeenLastCalledWith({
    kind: "delivery",
    identity: "delivery-two",
  });
  wrapper.unmount();
  const restored = mount(UsageView, {
    global: {
      plugins: [router],
      stubs: { BudgetPanel: true, PriceEditor: true },
    },
  });
  await flushPromises();
  expect(fetch).toHaveBeenLastCalledWith({
    kind: "delivery",
    identity: "delivery-two",
  });
  expect(restored.findAll(".call-details tbody > tr")).toHaveLength(
    costs.usage.calls.length * 2,
  );
  restored.unmount();
});
