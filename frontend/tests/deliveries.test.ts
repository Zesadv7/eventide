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
  repair: {
    control_run_id: "control",
    checks: ["test"],
    limits: { observed_tokens: 60_000 },
  },
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
it.each([60_000, 150_000])(
  "submits the chosen delivery limit %i",
  async (limit) => {
    reads();
    vi.mocked(deliveriesApi.list).mockResolvedValue([
      {
        ...detail(),
        repair: null,
        repair_result: null,
        repair_status: null,
        verification: null,
      },
    ]);
    const submit = vi
      .spyOn(deliveriesApi, "submit")
      .mockRejectedValue(new Error("fixture rejection"));
    const wrapper = await render();
    const input = wrapper.get('input[type="number"]');
    expect((input.element as HTMLInputElement).value).toBe("60000");
    await input.setValue(limit);
    await wrapper.findAll("textarea")[0]!.setValue("repair source");
    await wrapper.findAll("textarea")[1]!.setValue("python -m pytest");
    await wrapper.findAll("form")[1]!.trigger("submit");
    await flushPromises();
    const body = submit.mock.calls[0]![1].body;
    expect(body).toMatchObject({
      goal: "repair source",
      checks: ["python -m pytest"],
    });
    if (limit === 60_000)
      expect(body).not.toHaveProperty("observed_token_limit");
    else expect(body).toHaveProperty("observed_token_limit", limit);
    wrapper.unmount();
  },
);
it("appends an unverified checklist without replacing the goal or commands", async () => {
  reads();
  vi.mocked(deliveriesApi.list).mockResolvedValue([
    {
      ...detail(),
      repair: null,
      repair_result: null,
      repair_status: null,
      verification: null,
    },
  ]);
  const submit = vi
    .spyOn(deliveriesApi, "submit")
    .mockRejectedValue(new Error("fixture rejection"));
  const wrapper = await render();
  const target = wrapper.findAll("textarea")[0]!;
  expect(target.element.value).toBe("");
  await target.setValue("保留原目标和异常分支要求。");
  await wrapper.findAll("textarea")[1]!.setValue("first-check\nsecond-check");
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "追加验收覆盖清单模板")!
    .trigger("click");
  const goal = target.element.value;
  expect(goal).toMatch(/^保留原目标和异常分支要求。\n\n验收覆盖清单/);
  expect(goal).toContain("待填写，不代表已覆盖");
  expect(goal).toContain("未覆盖项（未验证）");
  await wrapper.findAll("form")[1]!.trigger("submit");
  await flushPromises();
  expect(submit).toHaveBeenCalledWith(project, {
    action: "repair",
    delivery: id,
    body: {
      goal,
      checks: ["first-check", "second-check"],
      image: null,
      max_rounds: 3,
    },
  });
  await target.setValue("目标".repeat(10_000));
  expect(
    wrapper
      .findAll("button")
      .find((b) => b.text() === "追加验收覆盖清单模板")!
      .attributes("disabled"),
  ).toBeDefined();
  wrapper.unmount();
});
it("shows the frozen coverage goal and ordered checks without claiming full coverage", async () => {
  reads();
  const goal = "目标：正常返回\n异常分支 -> 第 2 条\n未覆盖：超时；未验证";
  vi.mocked(deliveriesApi.list).mockResolvedValue([
    {
      ...detail(),
      repair: {
        ...detail().repair!,
        goal,
        checks: ["check-normal", "check-error"],
      },
    },
  ]);
  const wrapper = await render();
  expect(wrapper.get(".fixed-goal").text()).toBe(goal);
  expect(
    wrapper.findAll("ol.acceptance-commands li").map((check) => check.text()),
  ).toEqual(["check-normal", "check-error"]);
  expect(wrapper.text()).toContain("验收通过");
  expect(wrapper.text()).toContain("验收命令通过不代表覆盖全部目标");
  expect(wrapper.text()).toContain("未覆盖项仍未验证");
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
it("shows the frozen queued limit after refresh before the writer starts", async () => {
  reads();
  vi.mocked(deliveriesApi.list).mockResolvedValue([
    { ...detail(), repair: null, repair_result: null, repair_status: null },
  ]);
  vi.mocked(deliveriesApi.operations).mockResolvedValue([
    {
      run_id: "queued-run",
      session_id: "control-session",
      status: "queued",
      accepted: {
        action: "repair",
        delivery_id: id,
        observed_token_limit: 200_000,
      },
      error: null,
      reason: null,
    },
  ]);
  const wrapper = await render();
  expect(wrapper.text()).toContain("已接收 Token 软限：200000");
  expect(
    wrapper.get('input[type="number"]').attributes("disabled"),
  ).toBeDefined();
  wrapper.unmount();
});

it("clears accepted repair fields when switching to another delivery", async () => {
  reads();
  const pending = {
    ...detail(),
    repair: null,
    repair_result: null,
    repair_status: null,
    verification: null,
  };
  vi.mocked(deliveriesApi.list).mockResolvedValue([
    pending,
    {
      ...pending,
      delivery_id: "delivery_bbbbbbbbbbbbbbbb",
      name: "second-delivery",
    },
  ]);
  vi.spyOn(deliveriesApi, "submit").mockResolvedValue({
    run_id: "accepted",
    delivery_id: id,
    project_id: project,
    status_url: "/status",
    events_url: "/events",
  });
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  const wrapper = await render();
  await wrapper.findAll("textarea")[0]!.setValue("accepted first target");
  await wrapper.findAll("textarea")[1]!.setValue("first-target-test");
  await wrapper
    .get('article input:not([type="number"])')
    .setValue("first-image");
  await wrapper.get('article input[type="number"]').setValue(150000);
  await wrapper.get("article select").setValue(2);
  await wrapper.get("article form").trigger("submit");
  await flushPromises();
  await wrapper.findAll(".delivery-list button")[1]!.trigger("click");
  await flushPromises();
  expect(wrapper.get("article h3").text()).toBe("second-delivery");
  expect(
    wrapper.findAll("textarea").map((field) => field.element.value),
  ).toEqual(["", ""]);
  expect(
    wrapper.get<HTMLInputElement>('article input:not([type="number"])').element
      .value,
  ).toBe("");
  expect(
    wrapper.get<HTMLInputElement>('article input[type="number"]').element.value,
  ).toBe("60000");
  expect(wrapper.get<HTMLSelectElement>("article select").element.value).toBe(
    "3",
  );
  expect(confirm).not.toHaveBeenCalled();
  wrapper.unmount();
});

it("passes opt-in preparation to every Writer while allowing Docker verification", async () => {
  reads();
  vi.mocked(deliveriesApi.list).mockResolvedValue([
    {
      ...detail(),
      repair: null,
      repair_result: null,
      repair_status: null,
      verification: null,
    },
  ]);
  const submit = vi
    .spyOn(deliveriesApi, "submit")
    .mockRejectedValue(new Error("fixture rejection"));
  const wrapper = await render();
  const option = wrapper.get('[aria-label="每轮修复前准备知识索引"]');
  expect((option.element as HTMLInputElement).checked).toBe(false);
  await option.setValue(true);
  await wrapper.get(".index-preparation-options input").setValue(true);
  await wrapper.get('[aria-label="索引范围"]').setValue("all");
  await wrapper.findAll("textarea")[0]!.setValue("repair source");
  await wrapper.findAll("textarea")[1]!.setValue("python -m pytest");
  await wrapper.get("article input:not([type])").setValue("trusted-image");
  await wrapper.get("article form").trigger("submit");
  await flushPromises();
  expect(submit).toHaveBeenCalledWith(project, {
    action: "repair",
    delivery: id,
    body: {
      goal: "repair source",
      checks: ["python -m pytest"],
      image: "trusted-image",
      max_rounds: 3,
      knowledge_index: "prepare",
      code_structure: true,
      source_scope: "all",
    },
  });
  wrapper.unmount();
});
