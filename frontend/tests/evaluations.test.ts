import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  evaluationsApi,
  evaluationRequestState,
  type EvaluationDetail,
  type EvaluationInput,
  type EvaluationReceipt,
  type EvaluationReport,
  type EvaluationSuite,
} from "../src/api/evaluations";
import EvaluationsView from "../src/views/EvaluationsView.vue";
import { useWorkbench } from "../src/stores/workbench";

const suite: EvaluationSuite = {
  id: "engineering-v1",
  benchmark_id: "eventide-engineering",
  version: 1,
  description: "固定工程机制检查",
  manifest_sha256: "a".repeat(64),
  suite_sha256: "b".repeat(64),
  inputs_sha256: "c".repeat(64),
  split: null,
  total: 20,
  splits: { development: 12, holdout: 8 },
  supported_modes: ["offline", "live"],
  default_mode: "offline",
};
const body: EvaluationInput = {
  suite_id: "engineering-v1",
  suite_sha256: suite.suite_sha256,
  split: "development",
  mode: "offline",
};
let project = "",
  sequence = 0;
const receipt = (id = "evaluation-one"): EvaluationReceipt => ({
  evaluation_id: id,
  run_id: id,
  session_id: `session-${id}`,
  project_id: project,
  status: "accepted",
  status_url: `/api/evaluations/${id}`,
  events_url: `/api/runs/${id}/events`,
});
const detail = (
  id = "evaluation-one",
  extra: Partial<EvaluationDetail> = {},
): EvaluationDetail => ({
  ...receipt(id),
  suite_id: "engineering-v1",
  suite_version: 1,
  suite_sha256: suite.suite_sha256,
  split: "development",
  mode: "offline",
  measurement_kind: "scripted_mechanism_validation",
  billing_applicable: false,
  status: "completed",
  dispatch_state: null,
  queue_position: null,
  total: 12,
  attempted: 2,
  passed: 1,
  failed: 1,
  remaining: 10,
  current_case: null,
  cases: [
    {
      id: "passed-case",
      category: "single_file",
      split: "development",
      status: "completed",
      passed: true,
      reasons: [],
      duration_ms: 0,
    },
    {
      id: "failed-case",
      category: "single_file",
      split: "development",
      status: "unknown",
      passed: false,
      reasons: ["结果未知，停止验收"],
      duration_ms: null,
    },
    {
      id: "pending-case",
      category: "cross_file",
      split: "development",
      status: "pending",
      passed: null,
      reasons: [],
      duration_ms: null,
    },
  ],
  report_available: true,
  report_url: `/api/evaluations/${id}/report`,
  error: null,
  ...extra,
});
const report: EvaluationReport = {
  schema_version: 2,
  status: "completed",
  measurement_kind: "scripted_mechanism_validation",
  billing_applicable: false,
  passed: 1,
  total: 12,
  attempted: 2,
  remaining: 10,
  results: [],
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  project = `project-${++sequence}`;
  const store = useWorkbench();
  store.workspaces = [
    {
      workspace_id: "workspace",
      project_id: project,
      name: "选中的项目",
      path: "/fixture",
    },
    {
      workspace_id: "other-workspace",
      project_id: "other-project",
      name: "另一个项目",
      path: "/other",
    },
  ];
  store.selectedWorkspace = "workspace";
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
async function render(query = "") {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/evaluations", component: EvaluationsView },
      { path: "/elsewhere", component: { template: "<p>elsewhere</p>" } },
    ],
  });
  await router.push(`/evaluations${query}`);
  await router.isReady();
  const wrapper = mount(EvaluationsView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}
function mockReads(values = [detail()]) {
  vi.spyOn(evaluationsApi, "suites").mockResolvedValue([suite]);
  const list = vi.spyOn(evaluationsApi, "list").mockResolvedValue(values);
  const read = vi
    .spyOn(evaluationsApi, "detail")
    .mockImplementation(
      async (id) =>
        values.find((value) => value.evaluation_id === id) || detail(id),
    );
  return { list, read };
}

describe("durable evaluation requests", () => {
  it("recovers the original suite fingerprint after an unknown response and rejects a changed intent", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network lost"))
      .mockResolvedValueOnce(Response.json(receipt()));
    vi.stubGlobal("fetch", fetch);
    await expect(evaluationsApi.create(project, body)).rejects.toThrow(
      "network lost",
    );
    expect(evaluationRequestState(project).pending).toEqual(body);
    await expect(
      evaluationsApi.create(project, { ...body, suite_sha256: "d".repeat(64) }),
    ).rejects.toThrow("原任务集");
    expect(fetch).toHaveBeenCalledTimes(1);
    await evaluationsApi.create(
      project,
      evaluationRequestState(project).pending!,
    );
    expect(fetch.mock.calls[0]![1].headers["Idempotency-Key"]).toBe(
      fetch.mock.calls[1]![1].headers["Idempotency-Key"],
    );
    expect(evaluationRequestState(project).pending).toBeNull();
    expect(sessionStorage.length).toBe(0);
  });
  it("uses a new identity only after an explicitly rejected admission", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json(
          { detail: "full" },
          {
            status: 429,
            headers: { "X-Eventide-Mutation-Outcome": "rejected" },
          },
        ),
      )
      .mockResolvedValueOnce(Response.json(receipt()));
    vi.stubGlobal("fetch", fetch);
    await expect(evaluationsApi.create(project, body)).rejects.toThrow("full");
    expect(evaluationRequestState(project).pending).toBeNull();
    await evaluationsApi.create(project, body);
    expect(fetch.mock.calls[0]![1].headers["Idempotency-Key"]).not.toBe(
      fetch.mock.calls[1]![1].headers["Idempotency-Key"],
    );
  });
  it("keeps cancellation identity after an unknown result", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("lost stop"))
      .mockResolvedValueOnce(
        Response.json(detail("stop-me", { status: "interrupted" })),
      );
    vi.stubGlobal("fetch", fetch);
    await expect(evaluationsApi.cancel("stop-me")).rejects.toThrow("lost stop");
    await evaluationsApi.cancel("stop-me");
    expect(fetch.mock.calls[0]![0]).toBe("/api/evaluations/stop-me/cancel");
    expect(fetch.mock.calls[0]![1].headers["Idempotency-Key"]).toBe(
      fetch.mock.calls[1]![1].headers["Idempotency-Key"],
    );
  });
});

describe("offline evaluation page", () => {
  it("does not start automatically, separates mechanism results and preserves null versus zero", async () => {
    mockReads();
    const create = vi.spyOn(evaluationsApi, "create");
    vi.spyOn(evaluationsApi, "report").mockResolvedValue(report);
    const { wrapper } = await render();
    expect(create).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("不代表真实模型成功率");
    const rows = wrapper.findAll("tbody tr");
    expect(rows[0]!.text()).toContain("0 ms");
    expect(rows[1]!.text()).toContain("结果未知");
    expect(rows[2]!.text()).toContain("未确认");
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "读取完整报告")!
      .trigger("click");
    await flushPromises();
    expect(wrapper.get("pre").text()).toContain('"billing_applicable": false');
    expect(wrapper.text()).toContain("读取时的报告快照");
    wrapper.unmount();
  });
  it("restores a URL identity even when absent from the first history page", async () => {
    const { read } = mockReads([]);
    const { wrapper } = await render("?evaluation=older-run");
    expect(read).toHaveBeenCalledWith("older-run");
    expect(wrapper.get('[aria-label="评测运行详情"]').text()).toContain(
      "older-run",
    );
    wrapper.unmount();
  });
  it("ignores late list and detail responses after switching projects", async () => {
    const old = deferred<EvaluationDetail[]>(),
      oldDetail = deferred<EvaluationDetail>();
    mockReads([]);
    const oldValue = detail("old-project-run");
    vi.spyOn(evaluationsApi, "list").mockImplementation((owner) =>
      owner === project
        ? old.promise
        : Promise.resolve([
            detail("new-project-run", { project_id: "other-project" }),
          ]),
    );
    vi.spyOn(evaluationsApi, "detail").mockImplementation((id) =>
      id === "old-project-run"
        ? oldDetail.promise
        : Promise.resolve(detail(id, { project_id: "other-project" })),
    );
    const { wrapper } = await render("?evaluation=old-project-run");
    useWorkbench().selectedWorkspace = "other-workspace";
    await flushPromises();
    expect(wrapper.text()).toContain("new-project-run");
    old.resolve([oldValue]);
    oldDetail.resolve(oldValue);
    await flushPromises();
    expect(wrapper.text()).not.toContain("old-project-run");
    expect(wrapper.get('[aria-label="评测运行详情"]').text()).toContain(
      "new-project-run",
    );
    wrapper.unmount();
  });
  it("rejects details from another project even if a URL supplies the identity", async () => {
    mockReads([]);
    vi.spyOn(evaluationsApi, "detail").mockResolvedValue(
      detail("foreign", { project_id: "other-project" }),
    );
    const { wrapper } = await render("?evaluation=foreign");
    expect(wrapper.text()).toContain("不属于当前项目");
    expect(wrapper.find('[aria-label="评测运行详情"]').exists()).toBe(false);
    wrapper.unmount();
  });
  it("keeps a slow poll and invalidates an older queued read after cancellation", async () => {
    vi.useFakeTimers();
    const queued = detail("queued", {
      status: "queued",
      dispatch_state: "queued",
      queue_position: 1,
    });
    mockReads([queued]);
    const stale = deferred<EvaluationDetail>();
    vi.spyOn(evaluationsApi, "detail")
      .mockResolvedValueOnce(queued)
      .mockReturnValue(stale.promise);
    const cancel = vi.spyOn(evaluationsApi, "cancel").mockResolvedValue({
      ...queued,
      status: "interrupted",
      dispatch_state: null,
    });
    const { wrapper } = await render();
    await vi.advanceTimersByTimeAsync(6000);
    expect(evaluationsApi.detail).toHaveBeenCalledTimes(2);
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "取消评测排队")!
      .trigger("click");
    await flushPromises();
    expect(cancel).toHaveBeenCalledWith("queued");
    stale.resolve(queued);
    await flushPromises();
    expect(wrapper.get('[aria-label="评测运行详情"]').text()).toContain(
      "已中断",
    );
    expect(
      wrapper
        .findAll("button")
        .some((button) => button.text() === "取消评测排队"),
    ).toBe(false);
    wrapper.unmount();
  });
  it("keeps an in-flight admission across remount without submitting a second request", async () => {
    mockReads([]);
    const response = deferred<Response>();
    const fetch = vi.fn().mockReturnValue(response.promise);
    vi.stubGlobal("fetch", fetch);
    const first = await render();
    await first.wrapper.get("form").trigger("submit");
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    first.wrapper.unmount();
    const second = await render();
    expect(
      second.wrapper.get("#evaluation-split").attributes("disabled"),
    ).toBeDefined();
    expect(second.wrapper.text()).toContain("正在接收");
    response.resolve(Response.json(receipt("accepted-after-navigation")));
    await vi.waitFor(() =>
      expect(second.wrapper.text()).toContain("accepted-after-navigation"),
    );
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(second.router.currentRoute.value.query.evaluation).toBe(
      "accepted-after-navigation",
    );
    second.wrapper.unmount();
  });
  it("preserves the pending split and fingerprint after refresh instead of using a new catalog version", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("lost")));
    await expect(
      evaluationsApi.create(project, { ...body, split: "holdout" }),
    ).rejects.toThrow("lost");
    mockReads([]);
    vi.spyOn(evaluationsApi, "suites").mockResolvedValue([
      { ...suite, suite_sha256: "d".repeat(64) },
    ]);
    const create = vi
      .spyOn(evaluationsApi, "create")
      .mockResolvedValue(receipt());
    const { wrapper } = await render();
    expect(
      (wrapper.get("#evaluation-split").element as HTMLSelectElement).value,
    ).toBe("holdout");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(create).toHaveBeenCalledWith(project, { ...body, split: "holdout" });
    wrapper.unmount();
  });
});

it("labels a controlled live history as model evidence and links its sole usage owner", async () => {
  mockReads([
    detail("live-case", {
      mode: "live",
      measurement_kind: "provider_experiment",
      billing_applicable: true,
    }),
  ]);
  const { wrapper } = await render("?evaluation=live-case");
  await flushPromises();
  expect(wrapper.text()).toContain("真实模型实验 · 共享主服务预算");
  expect(wrapper.text()).toContain("独立验收通过");
  const usage = wrapper
    .findAll("a")
    .find((link) => link.text() === "查看实际调用用量");
  expect(usage?.attributes("href")).toContain("run=live-case");
  wrapper.unmount();
});

it("requires a fresh quote and explicit live confirmation without automatic calls", async () => {
  mockReads([]);
  const quote = vi.spyOn(evaluationsApi, "quote").mockResolvedValue({
    quote_hash: "d".repeat(64),
    project_id: project,
    suite_sha256: suite.suite_sha256,
    split: "development",
    case_count: 12,
    provider: "fixture",
    model: "fixture",
    planning_tokens: { input_tokens: 216000, output_tokens: 24000 },
    estimates: {},
    complete: false,
    assumptions: "规划估算不是账单上限",
  });
  const create = vi
    .spyOn(evaluationsApi, "create")
    .mockResolvedValue(receipt());
  const { wrapper } = await render();
  await wrapper.get("#evaluation-mode").setValue("live");
  expect(quote).not.toHaveBeenCalled();
  const start = () =>
    wrapper
      .findAll("button")
      .find((button) => button.text() === "运行一次真实模型实验")!;
  expect(start().attributes("disabled")).toBeDefined();
  await wrapper
    .findAll("button")
    .find((button) => button.text() === "读取模型与费用估算")!
    .trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("费用估算不完整或未知");
  expect(start().attributes("disabled")).toBeDefined();
  await wrapper.get('input[type="checkbox"]').setValue(true);
  expect(start().attributes("disabled")).toBeUndefined();
  await wrapper.get("#evaluation-split").setValue("holdout");
  expect(start().attributes("disabled")).toBeDefined();
  expect(create).not.toHaveBeenCalled();
  wrapper.unmount();
});
