import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  deliveriesApi,
  deliveryState,
  type Delivery,
  type Intent,
} from "../src/api/deliveries";
import DeliveriesView from "../src/views/DeliveriesView.vue";
import { useWorkbench } from "../src/stores/workbench";
let project = "",
  sequence = 0;
const id = "delivery_aaaaaaaaaaaaaaaa";
const detail = (): Delivery => ({
  delivery_id: id,
  workspace_id: "w",
  name: "repair-fixture",
  receipt: { checkout: "/isolated" },
  repair: { control_run_id: "control", checks: ["test"] },
  repair_result: {
    delivery_id: id,
    control_run_id: "control",
    reason: "passed",
    verified_candidates: 1,
    usage: {
      attempt_count: 2,
      reported_totals: {},
      estimated_totals: {},
      unknown_attempts: 0,
      incomplete_attempts: 0,
      complete: true,
    },
  },
  repair_status: "passed",
  rounds: [],
  verification: { status: "passed", patch_sha256: "a".repeat(64) },
  applied: false,
});
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  project = `delivery-project-${++sequence}`;
  const store = useWorkbench();
  store.workspaces = [
    {
      workspace_id: "w",
      project_id: project,
      name: "project",
      path: "/fixture",
    },
    {
      workspace_id: "other",
      project_id: "other",
      name: "other",
      path: "/other",
    },
  ];
  store.selectedWorkspace = "w";
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/deliveries", component: DeliveriesView },
      { path: "/usage", component: { template: "<p>usage</p>" } },
    ],
  });
  await router.push(`/deliveries?delivery=${id}`);
  await router.isReady();
  const wrapper = mount(DeliveriesView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}
function reads() {
  vi.spyOn(deliveriesApi, "operations").mockResolvedValue([]);
  vi.spyOn(deliveriesApi, "list").mockResolvedValue([detail()]);
  vi.spyOn(deliveriesApi, "patch").mockResolvedValue({
    delivery_id: id,
    patch_sha256: "a".repeat(64),
    patch: "+fix",
  });
}
it("requires explicit patch inspection and review before adoption", async () => {
  reads();
  const submit = vi
    .spyOn(deliveriesApi, "submit")
    .mockRejectedValue(new Error("fixture rejection"));
  const wrapper = await render();
  expect(wrapper.text()).toContain("验收通过");
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "查看验收补丁")!
    .trigger("click");
  await flushPromises();
  const apply = wrapper
    .findAll("button")
    .find((b) => b.text() === "采纳已检查的补丁")!;
  expect(apply.attributes("disabled")).toBeDefined();
  await wrapper.get('input[type="checkbox"]').setValue(true);
  await apply.trigger("click");
  await flushPromises();
  expect(submit).toHaveBeenCalledWith(project, {
    action: "apply",
    delivery: id,
    body: { patch_sha256: "a".repeat(64) },
  });
  expect(wrapper.text()).toContain("fixture rejection");
  wrapper.unmount();
});
it("never presents a late response from the previous project", async () => {
  reads();
  let resolve!: (value: Delivery[]) => void;
  vi.mocked(deliveriesApi.list)
    .mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    )
    .mockResolvedValue([]);
  const wrapper = await render();
  useWorkbench().selectedWorkspace = "other";
  await flushPromises();
  resolve([detail()]);
  await flushPromises();
  expect(wrapper.text()).not.toContain("repair-fixture");
  wrapper.unmount();
});
it("retains the original uncertain request and idempotency identity", async () => {
  const keys: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url, options) => {
      keys.push(options.headers["Idempotency-Key"]);
      if (keys.length === 1) throw new Error("lost response");
      return new Response(
        JSON.stringify({
          run_id: "run",
          delivery_id: id,
          project_id: project,
          status_url: "/status",
          events_url: "/events",
        }),
        { status: 202 },
      );
    }),
  );
  const intent: Intent = { action: "prepare", body: { name: "fixture" } };
  await expect(deliveriesApi.submit(project, intent)).rejects.toThrow();
  expect(deliveryState(project).pending).toEqual(intent);
  await expect(
    deliveriesApi.submit(project, {
      action: "prepare",
      body: { name: "changed" },
    }),
  ).rejects.toThrow("原交付请求");
  await deliveriesApi.submit(project, intent);
  expect(keys[0]).toBe(keys[1]);
  expect(deliveryState(project).pending).toBeNull();
});
it("refresh with uncertain admission locks forms and offers exact retry", async () => {
  reads();
  sessionStorage.setItem(
    `eventide.delivery-pending.v1.${project}`,
    JSON.stringify({ action: "prepare", body: { name: "frozen" } }),
  );
  const wrapper = await render();
  expect(
    wrapper.get('input[placeholder="fix-login"]').attributes("disabled"),
  ).toBeDefined();
  expect(wrapper.text()).toContain("重试原交付请求");
  wrapper.unmount();
});
