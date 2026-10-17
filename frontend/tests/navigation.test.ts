import { afterEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { defineComponent, ref } from "vue";
import {
  createRouter,
  createMemoryHistory,
  createWebHashHistory,
} from "vue-router";
import PageBack from "../src/components/PageBack.vue";
import PanelClose from "../src/components/PanelClose.vue";
import { useUnsavedChanges } from "../src/composables/useUnsavedChanges";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("uses an accessible non-submit close control", async () => {
  const wrapper = mount(PanelClose, { props: { label: "关闭预览" } });
  expect(wrapper.get("button").attributes("type")).toBe("button");
  expect(wrapper.get("button").attributes("aria-label")).toBe("关闭预览");
  await wrapper.get("button").trigger("click");
  expect(wrapper.emitted("close")).toHaveLength(1);
  wrapper.unmount();
});
it("combines nested dirty drafts into one route prompt and protects unload", async () => {
  const dirty = ref(true);
  const Draft = defineComponent({
    setup() {
      useUnsavedChanges(() => dirty.value);
      return () => "draft";
    },
  });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/settings", component: Draft },
      { path: "/chat", component: Draft },
    ],
  });
  await router.push("/settings");
  const wrapper = mount(
    defineComponent({ components: { Draft }, template: "<Draft/><Draft/>" }),
    { global: { plugins: [router] } },
  );
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  await router.push("/chat");
  expect(router.currentRoute.value.path).toBe("/settings");
  expect(confirm).toHaveBeenCalledTimes(1);
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  dirty.value = false;
  await router.push("/chat");
  expect(router.currentRoute.value.path).toBe("/chat");
  wrapper.unmount();
  const cleanEvent = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(cleanEvent);
  expect(cleanEvent.defaultPrevented).toBe(false);
});
it.each([createWebHashHistory, createMemoryHistory])(
  "returns only through known successful internal history and preserves cancelled back (%s)",
  async (createHistory) => {
    const history = createHistory();
    const Empty = { template: "<p>page</p>" };
    const router = createRouter({
      history,
      routes: ["/chat", "/settings", "/usage"].map((path) => ({
        path,
        component: Empty,
      })),
    });
    await router.push("/chat");
    const wrapper = mount(PageBack, { global: { plugins: [router] } });
    await router.push("/settings");
    await router.push("/usage");
    const abort = router.beforeEach(() => false);
    await wrapper.get("button").trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.path).toBe("/usage");
    abort();
    await vi.waitFor(async () => {
      await flushPromises();
      expect(history.location).toBe("/usage");
    });
    await wrapper.get("button").trigger("click");
    await vi.waitFor(() =>
      expect(router.currentRoute.value.path).toBe("/settings"),
    );
    router.forward();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.path).toBe("/usage"),
    );
    await wrapper.get("button").trigger("click");
    await vi.waitFor(() =>
      expect(router.currentRoute.value.path).toBe("/settings"),
    );
    wrapper.unmount();
    history.destroy();
  },
);
it("falls back to chat on a direct page entry", async () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ["/chat", "/settings"].map((path) => ({
      path,
      component: { template: "<p/>" },
    })),
  });
  await router.push("/settings");
  const wrapper = mount(PageBack, { global: { plugins: [router] } });
  await wrapper.get("button").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.path).toBe("/chat");
  wrapper.unmount();
});

it("keeps generation, budget and price edits guarded without persisting draft secrets", async () => {
  const { createPinia, setActivePinia } = await import("pinia");
  const { api } = await import("../src/api/client");
  const { budgetsApi } = await import("../src/api/budgets");
  const { provider, generation, budgetStatus } = await import("./fixtures");
  const { default: SettingsView } =
    await import("../src/views/SettingsView.vue");
  const { default: BudgetPanel } =
    await import("../src/components/BudgetPanel.vue");
  const { default: PriceEditor } =
    await import("../src/components/PriceEditor.vue");
  setActivePinia(createPinia());
  sessionStorage.clear();
  localStorage.clear();
  vi.spyOn(api, "provider").mockResolvedValue(provider);
  vi.spyOn(api, "generation").mockResolvedValue(generation);
  vi.spyOn(budgetsApi, "status").mockResolvedValue(budgetStatus);
  vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ["/settings", "/chat"].map((path) => ({
      path,
      component: { template: "<p/>" },
    })),
  });
  await router.push("/settings");
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  const wrapper = mount(SettingsView, { global: { plugins: [router] } });
  await flushPromises();
  await wrapper.get("#mode-model-temperature").setValue("value");
  await router.push("/chat");
  expect(router.currentRoute.value.path).toBe("/settings");
  expect(confirm).toHaveBeenCalledTimes(1);
  await wrapper
    .get('input[type="password"]')
    .setValue("secret-draft-never-persist");
  expect(JSON.stringify({ ...sessionStorage, ...localStorage })).not.toContain(
    "secret-draft-never-persist",
  );
  wrapper.unmount();
  const budget = mount(BudgetPanel, {
    props: { scope: { kind: "project", identity: "project-1" } },
    global: { plugins: [router] },
  });
  await flushPromises();
  await budget.get('select[aria-label="预算开关"]').setValue(true);
  await router.push("/chat");
  expect(router.currentRoute.value.path).toBe("/settings");
  budget.unmount();
  const price = mount(PriceEditor, { global: { plugins: [router] } });
  await flushPromises();
  await price.get('input[inputmode="decimal"]').setValue("12.5");
  await router.push("/chat");
  expect(router.currentRoute.value.path).toBe("/settings");
  price.unmount();
  await router.push("/chat");
  expect(router.currentRoute.value.path).toBe("/chat");
});

it("guards query identity changes only for drafts whose context depends on them", async () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/usage", component: { template: "<p/>" } }],
  });
  await router.push("/usage?run=one");
  const Draft = defineComponent({
    setup() {
      useUnsavedChanges(() => true, { guardQueryChanges: true });
      return () => "draft";
    },
  });
  const wrapper = mount(Draft, { global: { plugins: [router] } });
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  await router.push("/usage?run=two");
  expect(router.currentRoute.value.query.run).toBe("one");
  expect(confirm).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});
