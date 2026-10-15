import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BudgetPanel from "../src/components/BudgetPanel.vue";
import CostSummary from "../src/components/CostSummary.vue";
import PriceEditor from "../src/components/PriceEditor.vue";
import UsageView from "../src/views/UsageView.vue";
import { budgetsApi, type BudgetStatus } from "../src/api/budgets";
import { useWorkbench } from "../src/stores/workbench";
import { budgetStatus, costs } from "./fixtures";
const scope = { kind: "project" as const, identity: "project-1" };
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("budget and cost controls", () => {
  it("allows a new reconciliation after a confirmed rejection but retains unknown conflict identity", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Wait for attempt" }), {
          status: 409,
          headers: { "X-Eventide-Mutation-Outcome": "rejected" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Outcome unknown" }), {
          status: 409,
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            action: "keep_unknown",
            attempt: budgetStatus.reservations[0],
          }),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    const body = {
      action: "keep_unknown" as const,
      reason: "Checked provider",
    };
    await expect(budgetsApi.resolve("attempt-1", body)).rejects.toThrow("Wait");
    await expect(budgetsApi.resolve("attempt-1", body)).rejects.toThrow(
      "Outcome",
    );
    await budgetsApi.resolve("attempt-1", body);
    const keys = fetch.mock.calls.map(
      (call) => call[1].headers["Idempotency-Key"],
    );
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys[1]).toBe(keys[2]);
  });
  it("does not starve a slow successful budget or usage response with overlapping polls", async () => {
    vi.useFakeTimers();
    vi.spyOn(budgetsApi, "status").mockImplementation(
      () =>
        new Promise((resolve) => setTimeout(() => resolve(budgetStatus), 5000)),
    );
    vi.spyOn(budgetsApi, "costs").mockImplementation(
      () => new Promise((resolve) => setTimeout(() => resolve(costs), 5000)),
    );
    vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
    useWorkbench().selectedSession = "session-1";
    const budget = mount(BudgetPanel, { props: { scope } });
    const usage = mount(UsageView);
    await vi.advanceTimersByTimeAsync(6000);
    await flushPromises();
    expect(budget.text()).not.toContain("正在读取预算…");
    expect(usage.text()).toContain("统计不完整");
    expect(budgetsApi.status).toHaveBeenCalledTimes(1);
    expect(budgetsApi.costs).toHaveBeenCalledTimes(1);
    budget.unmount();
    usage.unmount();
  });
  it("preserves a newer draft even if an earlier save response arrives later", async () => {
    vi.spyOn(budgetsApi, "status").mockResolvedValue(budgetStatus);
    let finish!: (value: BudgetStatus) => void;
    vi.spyOn(budgetsApi, "configure").mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const wrapper = mount(BudgetPanel, { props: { scope } });
    await flushPromises();
    await wrapper.find("select").setValue("true");
    await wrapper.find('input[type="number"]').setValue("1000");
    await wrapper.find("form").trigger("submit");
    // The user can keep editing, but cannot submit a second overlapping save.
    expect(wrapper.find("button.primary").attributes("disabled")).toBeDefined();
    await wrapper.find('input[type="number"]').setValue("2000");
    finish({
      ...budgetStatus,
      budgets: [
        {
          ...budgetStatus.budgets[0]!,
          token_limit: 1000,
          configuration_hash: "b".repeat(64),
        },
      ],
    });
    await flushPromises();
    expect(
      (wrapper.find('input[type="number"]').element as HTMLInputElement).value,
    ).toBe("2000");
    wrapper.unmount();
  });
  it("keeps unknown cost separate and never sums currencies", () => {
    const wrapper = mount(CostSummary, {
      props: { costs: { ...costs, known_costs: { CNY: "0.12", USD: "0.03" } } },
    });
    expect(wrapper.text()).toContain("估算不完整");
    expect(wrapper.text()).toContain("CNY 已知费用");
    expect(wrapper.text()).toContain("USD 已知费用");
    expect(wrapper.text()).not.toContain("0.15");
  });
  it("preserves a zero limit, explicit disabled state and observed revision", async () => {
    vi.spyOn(budgetsApi, "status").mockResolvedValue(budgetStatus);
    const save = vi
      .spyOn(budgetsApi, "configure")
      .mockResolvedValue(budgetStatus);
    const wrapper = mount(BudgetPanel, { props: { scope } });
    await flushPromises();
    await wrapper.find("select").setValue("true");
    await wrapper.find('input[type="number"]').setValue("0");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(save).toHaveBeenLastCalledWith(scope, 0, "a".repeat(64));
    await wrapper.find("select").setValue("false");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(save).toHaveBeenLastCalledWith(scope, null, "a".repeat(64));
    wrapper.unmount();
  });
  it("never overwrites an edited configuration revision while polling", async () => {
    vi.useFakeTimers();
    const status = vi
      .spyOn(budgetsApi, "status")
      .mockResolvedValue(structuredClone(budgetStatus));
    const save = vi
      .spyOn(budgetsApi, "configure")
      .mockResolvedValue(budgetStatus);
    const wrapper = mount(BudgetPanel, { props: { scope } });
    await flushPromises();
    await wrapper.find("select").setValue("true");
    await wrapper.find('input[type="number"]').setValue("1000");
    const changed = structuredClone(budgetStatus);
    changed.budgets[0]!.configuration_hash = "b".repeat(64);
    changed.budgets[0]!.token_limit = 500;
    status.mockResolvedValue(changed);
    await vi.advanceTimersByTimeAsync(4000);
    await flushPromises();
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(save).toHaveBeenCalledWith(scope, 1000, "a".repeat(64));
    wrapper.unmount();
  });
  it("ignores a delayed budget response after switching projects", async () => {
    let finish!: (status: BudgetStatus) => void;
    vi.spyOn(budgetsApi, "status")
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finish = resolve;
        }),
      )
      .mockResolvedValue({
        ...budgetStatus,
        budgets: [
          {
            ...budgetStatus.budgets[0]!,
            identity: "project-2",
            actual_tokens: 777,
          },
        ],
      });
    const wrapper = mount(BudgetPanel, { props: { scope } });
    await wrapper.setProps({
      scope: { kind: "project", identity: "project-2" },
    });
    await flushPromises();
    finish(budgetStatus);
    await flushPromises();
    expect(wrapper.text()).toContain("777");
    wrapper.unmount();
  });
  it("keeps unknown reservations on explicit review without fabricating usage", async () => {
    vi.spyOn(budgetsApi, "status").mockResolvedValue(budgetStatus);
    const resolve = vi.spyOn(budgetsApi, "resolve").mockResolvedValue({
      action: "keep_unknown",
      attempt: budgetStatus.reservations[0]!,
    });
    const wrapper = mount(BudgetPanel, { props: { scope } });
    await flushPromises();
    expect(resolve).not.toHaveBeenCalled();
    await wrapper
      .findAll("button")
      .find((item) => item.text() === "核对")!
      .trigger("click");
    await wrapper.find('input[maxlength="256"]').setValue("供应商尚未提供用量");
    await wrapper.findAll("form")[1]!.trigger("submit");
    await flushPromises();
    expect(resolve).toHaveBeenCalledWith("attempt-1", {
      action: "keep_unknown",
      reason: "供应商尚未提供用量",
    });
    wrapper.unmount();
  });
  it("uses immutable project identity for all-history queries", async () => {
    vi.spyOn(budgetsApi, "costs").mockResolvedValue(costs);
    vi.spyOn(budgetsApi, "status").mockResolvedValue(budgetStatus);
    vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
    const store = useWorkbench();
    store.selectedWorkspace = "copy-1";
    store.selectedSession = "session-1";
    store.workspaces = [
      {
        workspace_id: "copy-1",
        project_id: "project-1",
        path: "/fixture",
        name: "copy",
      },
    ];
    const wrapper = mount(UsageView);
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((item) => item.text() === "当前项目全部对话")!
      .trigger("click");
    await flushPromises();
    expect(budgetsApi.costs).toHaveBeenLastCalledWith(scope);
    wrapper.unmount();
  });
  it("retains immutable price identity across an uncertain retry", async () => {
    vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
    const save = vi
      .spyOn(budgetsApi, "registerPrice")
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValue({} as never);
    const wrapper = mount(PriceEditor);
    await flushPromises();
    await wrapper.find('input[maxlength="256"]').setValue("model");
    await wrapper.find('input[inputmode="decimal"]').setValue("0.25");
    await wrapper.find("form").trigger("submit");
    await vi.waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    await flushPromises();
    await wrapper.find("form").trigger("submit");
    await vi.waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    expect(save.mock.calls[0]![0]).toEqual(save.mock.calls[1]![0]);
    wrapper.unmount();
  });
  it("reuses mutation identity after a lost response and does not persist payloads", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValue({ ok: true, json: async () => budgetStatus });
    vi.stubGlobal("fetch", fetch);
    await expect(
      budgetsApi.configure(scope, 1234, "a".repeat(64)),
    ).rejects.toThrow("offline");
    await budgetsApi.configure(scope, 1234, "a".repeat(64));
    expect(fetch.mock.calls[0]![1].headers["Idempotency-Key"]).toBe(
      fetch.mock.calls[1]![1].headers["Idempotency-Key"],
    );
    expect(JSON.stringify(sessionStorage)).not.toContain("project-1");
    expect(sessionStorage.length).toBe(0);
  });
});
