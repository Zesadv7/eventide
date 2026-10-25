import { afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import RunDiagnostics from "../src/components/RunDiagnostics.vue";
import RunDiagnosticsView from "../src/views/RunDiagnosticsView.vue";
import { router } from "../src/router";

afterEach(() => vi.unstubAllGlobals());

const report = {
  report_version: 1,
  source: "eventide.event_log",
  run_id: "r-1",
  high_water: 4,
  facts_available: true,
  status: "interrupted",
  stop_reason: "unknown_tool_outcome",
  mode: "auto",
  provider: "scripted",
  model: "scripted",
  started_at: 1,
  ended_at: 9,
  counts: { steps: 1, tool_calls: 1, tools_completed: 0, tools_unknown: 1 },
  summary: {
    what_happened: "Run 状态：已中断。",
    where_stopped: "停止于：工具结果未知",
    completed: "停止前：已完成 0 次成功工具调用。",
    unknowns: ["工具结果未知：清理未确认"],
    next_steps: ["先核查未知结果工具的实际副作用，再决定继续或重做"],
  },
  known_causes: [
    {
      code: "stop_reason",
      label: "停止原因",
      detail: "工具结果未知",
      ref: {
        event_type: "run.interrupted",
        at: 9,
        call_id: null,
        attempt_id: null,
      },
    },
  ],
  pending: [
    {
      code: "tool_unknown",
      label: "工具结果未知",
      detail: "清理未确认",
      ref: {
        event_type: "tool.outcome_unknown",
        at: 8,
        call_id: "c1",
        attempt_id: null,
      },
    },
  ],
  tools: [
    {
      call_id: "c1",
      name: "bash",
      readonly: false,
      status: "unknown",
      detail: "清理未确认",
      duration_ms: null,
      at: 7,
    },
  ],
  attempts: [
    {
      attempt_id: "a1",
      attempt: 1,
      kind: "agent",
      status: "completed",
      usage_source: "reported",
      model: "scripted",
      started_at: 2,
      finished_at: 3,
    },
  ],
  unavailable: [],
  notes: ["当前报告版本不包含任务图/恢复中心关系与恢复动作入口。"],
  truncated: false,
};

function stubReport(value: unknown, status = 200) {
  return vi.fn(async () => new Response(JSON.stringify(value), { status }));
}

it("auto-loads the report and binds downloads to its high-water mark", async () => {
  const fetcher = vi.fn(
    async (_url: RequestInfo | URL) =>
      new Response(JSON.stringify(report), { status: 200 }),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mount(RunDiagnostics, { props: { runId: "r-1" } });
  expect(fetcher).toHaveBeenCalledTimes(1);
  await flushPromises();
  expect(String(fetcher.mock.calls[0]?.[0])).toBe("/api/runs/r-1/diagnostics");
  const text = wrapper.text();
  for (const section of [
    "发生了什么",
    "停在哪里",
    "完成了什么",
    "哪些结果未知",
    "已知原因",
    "待查事项",
    "下一步可做什么",
  ]) {
    expect(text).toContain(section);
  }
  expect(text).toContain("事件高水位 4");
  const links = wrapper.findAll("a").map((a) => a.attributes("href"));
  expect(links).toContain(
    "/api/runs/r-1/diagnostics/download?format=json&high_water=4",
  );
  expect(links).toContain(
    "/api/runs/r-1/diagnostics/download?format=markdown&high_water=4",
  );
  wrapper.unmount();
});

it("shows missing facts without fabricating a report", async () => {
  vi.stubGlobal(
    "fetch",
    stubReport({
      ...report,
      facts_available: false,
      status: "unknown",
      unavailable: ["Run 事件记录缺失，无法还原执行过程"],
    }),
  );
  const wrapper = mount(RunDiagnostics, { props: { runId: "empty" } });
  await flushPromises();
  const alert = wrapper.get('[role="alert"]');
  expect(alert.text()).toContain("缺少可用事件事实");
  expect(wrapper.find("h3").exists()).toBe(false);
  expect(wrapper.findAll("a").length).toBe(0);
  wrapper.unmount();
});

it("surfaces report errors instead of a partial report", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            code: "not_found",
            message: "Run not found",
            detail: "Run not found",
          }),
          { status: 404 },
        ),
    ),
  );
  const wrapper = mount(RunDiagnostics, { props: { runId: "gone" } });
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("Run not found");
  expect(wrapper.find("h3").exists()).toBe(false);
  wrapper.unmount();
});

it("regeneration moves to a new high-water mark and rebinding links", async () => {
  let call = 0;
  const fetcher = vi.fn(async () => {
    call += 1;
    return new Response(
      JSON.stringify({ ...report, high_water: call === 1 ? 4 : 5 }),
      { status: 200 },
    );
  });
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mount(RunDiagnostics, { props: { runId: "r-1" } });
  await flushPromises();
  expect(wrapper.text()).toContain("事件高水位 4");
  await wrapper.get("button").trigger("click");
  await flushPromises();
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(wrapper.text()).toContain("事件高水位 5");
  expect(
    wrapper
      .findAll("a")
      .some((a) =>
        (a.attributes("href") || "").endsWith("format=json&high_water=5"),
      ),
  ).toBe(true);
  wrapper.unmount();
});

it("drops a stale response after the run identity changes", async () => {
  const resolvers: Array<(value: Response) => void> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolvers.push(resolve);
        }),
    ),
  );
  const wrapper = mount(RunDiagnostics, { props: { runId: "old" } });
  await wrapper.setProps({ runId: "new" });
  expect(resolvers.length).toBe(2);
  resolvers[0](new Response(JSON.stringify(report), { status: 200 }));
  await flushPromises();
  expect(wrapper.text()).not.toContain("发生了什么");
  expect(wrapper.get("button").attributes("disabled")).toBeDefined();
  resolvers[1](new Response(JSON.stringify(report), { status: 200 }));
  await flushPromises();
  expect(wrapper.text()).toContain("发生了什么");
  wrapper.unmount();
});

it("exposes the diagnostics page route and renders the selected run", async () => {
  const routes = router.getRoutes().map((route) => route.path);
  expect(routes).toContain("/runs/:runId/diagnostics");
  const local = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: "/runs/:runId/diagnostics",
        component: RunDiagnosticsView,
      },
    ],
  });
  await local.push("/runs/r-9/diagnostics");
  vi.stubGlobal("fetch", stubReport({ ...report, run_id: "r-9" }));
  const wrapper = mount(RunDiagnosticsView, {
    global: { plugins: [local] },
  });
  await flushPromises();
  expect(wrapper.text()).toContain("运行故障报告");
  expect(wrapper.text()).toContain("发生了什么");
  wrapper.unmount();
});
