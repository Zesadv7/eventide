import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createRouter, createMemoryHistory } from "vue-router";
import QueueView from "../src/views/QueueView.vue";
import { queueApi, type QueueSnapshot } from "../src/api/queue";
import { api } from "../src/api/client";
import { useWorkbench } from "../src/stores/workbench";

const snapshot: QueueSnapshot = {
  limits: { active_roots: 4, model_requests: 2, waiting_roots: 20 },
  counts: {
    active_roots: 4,
    waiting_roots: 1,
    preparing_admissions: 0,
    active_models: 2,
    waiting_models: 2,
  },
  active: [],
  queued: [
    {
      run_id: "queued-run",
      session_id: "queued-session",
      project_id: "shared-project",
      workspace_id: "source-workspace",
      execution: { kind: "worktree" },
      admitted_at: 1000,
      position: 1,
      status: "queued",
      dispatch_state: "queued",
    },
  ],
};
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
});
afterEach(() => vi.useRealTimers());
async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: QueueView },
      { path: "/chat/:id", component: { template: "<div>conversation</div>" } },
    ],
  });
  await router.push("/");
  await router.isReady();
  return {
    router,
    wrapper: mount(QueueView, { global: { plugins: [router] } }),
  };
}
describe("global persistent task queue", () => {
  it("shows service-wide limits and cancels the precise queued Run", async () => {
    const status = vi.spyOn(queueApi, "status").mockResolvedValue(snapshot);
    const cancel = vi.spyOn(api, "cancel").mockResolvedValue({});
    const { wrapper } = await render();
    await flushPromises();
    expect(wrapper.text()).toContain("整个服务共用");
    expect(wrapper.text()).toContain("4 / 4");
    expect(wrapper.text()).toContain("1 / 20");
    expect(wrapper.text()).toContain("独立工作副本");
    status.mockResolvedValue({ ...snapshot, queued: [] });
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "取消排队")!
      .trigger("click");
    await flushPromises();
    expect(cancel).toHaveBeenCalledWith("queued-run");
    expect(wrapper.text()).not.toContain("queued-run");
    wrapper.unmount();
  });
  it("opens the owning source project and Session rather than the currently selected one", async () => {
    vi.spyOn(queueApi, "status").mockResolvedValue(snapshot);
    const store = useWorkbench();
    store.selectedWorkspace = "another-project";
    const workspace = vi
      .spyOn(store, "selectWorkspace")
      .mockResolvedValue(undefined);
    const session = vi
      .spyOn(store, "selectSession")
      .mockResolvedValue(undefined);
    const { wrapper, router } = await render();
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "打开对话")!
      .trigger("click");
    await flushPromises();
    expect(workspace).toHaveBeenCalledWith("source-workspace");
    expect(session).toHaveBeenCalledWith("queued-session");
    expect(router.currentRoute.value.path).toBe("/chat/queued-session");
    wrapper.unmount();
  });
  it("keeps slow reads alive across polling ticks and stops polling on unmount", async () => {
    vi.useFakeTimers();
    const status = vi
      .spyOn(queueApi, "status")
      .mockImplementation(
        () =>
          new Promise((resolve) => setTimeout(() => resolve(snapshot), 5000)),
      );
    const { wrapper } = await render();
    await vi.advanceTimersByTimeAsync(6000);
    await flushPromises();
    expect(wrapper.text()).toContain("queued-run");
    expect(status).toHaveBeenCalledTimes(2);
    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(10000);
    expect(status).toHaveBeenCalledTimes(2);
  });
});
