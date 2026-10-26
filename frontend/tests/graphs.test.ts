import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import GraphPanel from "../src/components/GraphPanel.vue";
import { graphsApi, type AgentGraph } from "../src/api/graphs";

function graph(extra: Partial<AgentGraph> = {}): AgentGraph {
  return {
    graph_id: "g1",
    root_run_id: "r1",
    session_id: "s1",
    title: "调查登录失败",
    state: "ready",
    cancelled: false,
    started: false,
    nodes: [
      {
        id: "inspect",
        kind: "readonly",
        goal: "读取入口",
        depends_on: [],
        state: "ready",
        claim: null,
        result: null,
      },
      {
        id: "review",
        kind: "readonly",
        goal: "核对前置发现",
        depends_on: ["inspect"],
        state: "pending",
        claim: null,
        result: null,
      },
    ],
    counts: { total: 2, running: 0, completed: 0 },
    ...extra,
  };
}
const projection = (items: AgentGraph[], run = "r1") => ({
  run_id: run,
  graphs: items,
});
const mountPanel = () =>
  mount(GraphPanel, {
    props: { runId: "r1" },
    global: {
      stubs: {
        RouterLink: {
          name: "RouterLink",
          props: ["to"],
          template: "<a><slot /></a>",
        },
      },
    },
  });
function button(wrapper: ReturnType<typeof mountPanel>, text: string) {
  return wrapper.findAll("button").find((item) => item.text() === text)!;
}

beforeEach(() => sessionStorage.clear());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("saves a definition with dependencies and waits for an explicit start", async () => {
  vi.spyOn(graphsApi, "list").mockResolvedValue(projection([]));
  const create = vi.spyOn(graphsApi, "create").mockResolvedValue(graph());
  const advance = vi.spyOn(graphsApi, "advance");
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find('[aria-label="任务图名称"]').setValue("调查登录失败");
  await wrapper.find('[aria-label="task_1 的任务"]').setValue("读取入口");
  await button(wrapper, "添加任务").trigger("click");
  await wrapper.find('[aria-label="task_2 的任务"]').setValue("核对前置发现");
  await wrapper.find('[aria-label="task_2 依赖 task_1"]').setValue(true);
  await wrapper.find("form").trigger("submit");
  await flushPromises();
  expect(create).toHaveBeenCalledWith("r1", {
    title: "调查登录失败",
    nodes: [
      { id: "task_1", kind: "readonly", goal: "读取入口", depends_on: [] },
      {
        id: "task_2",
        kind: "readonly",
        goal: "核对前置发现",
        depends_on: ["task_1"],
      },
    ],
  });
  expect(advance).not.toHaveBeenCalled();
  expect(wrapper.text()).toContain("方案已保存");
  expect(wrapper.text()).toContain("inspect → review");
  expect(button(wrapper, "开始调查").exists()).toBe(true);
  wrapper.unmount();
});

it("starts the saved graph and preserves the server's actual stopping state", async () => {
  vi.spyOn(graphsApi, "list").mockResolvedValue(projection([graph()]));
  const running = graph({
    state: "running",
    started: true,
    counts: { total: 2, running: 1, completed: 0 },
  });
  const advance = vi.spyOn(graphsApi, "advance").mockResolvedValue(running);
  const cancel = vi
    .spyOn(graphsApi, "cancel")
    .mockResolvedValue({ ...running, cancelled: true, state: "cancelling" });
  const wrapper = mountPanel();
  await flushPromises();
  await button(wrapper, "开始调查").trigger("click");
  await flushPromises();
  expect(advance).toHaveBeenCalledWith("g1");
  expect(wrapper.text()).toContain("执行中");
  await button(wrapper, "停止任务图").trigger("click");
  await flushPromises();
  expect(cancel).toHaveBeenCalledWith("g1");
  expect(wrapper.text()).toContain("正在等待已启动的调查停止并清理快照");
  expect(wrapper.text()).toContain("停止中");
  expect(wrapper.text()).not.toContain("开始调查");
  wrapper.unmount();
});

it("keeps the definition on a rejected save and removes deleted dependencies", async () => {
  vi.spyOn(graphsApi, "list").mockResolvedValue(projection([]));
  const create = vi
    .spyOn(graphsApi, "create")
    .mockRejectedValue(new Error("存在依赖环，请检查"));
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find('[aria-label="任务图名称"]').setValue("保留这个方案");
  await wrapper.find('[aria-label="task_1 的任务"]').setValue("第一项调查");
  await button(wrapper, "添加任务").trigger("click");
  await wrapper.find('[aria-label="task_2 的任务"]').setValue("第二项调查");
  await wrapper.find('[aria-label="task_2 依赖 task_1"]').setValue(true);
  await button(wrapper, "删除 task_1").trigger("click");
  await wrapper.find("form").trigger("submit");
  await flushPromises();
  expect(create.mock.calls[0]![1].nodes).toEqual([
    { id: "task_2", kind: "readonly", goal: "第二项调查", depends_on: [] },
  ]);
  expect(
    wrapper.find<HTMLInputElement>('[aria-label="任务图名称"]').element.value,
  ).toBe("保留这个方案");
  expect(
    wrapper.find<HTMLTextAreaElement>('[aria-label="task_2 的任务"]').element
      .value,
  ).toBe("第二项调查");
  expect(wrapper.get('[role="alert"]').text()).toBe("存在依赖环，请检查");
  wrapper.unmount();
});

it("loads saved results after remount, shows literal HTML and links the exact child session", async () => {
  const value = graph({
    state: "completed",
    started: true,
    nodes: [
      {
        id: "inspect",
        kind: "readonly",
        goal: "读取入口",
        depends_on: [],
        state: "completed",
        claim: { graph_id: "g1", node_id: "inspect", run_id: "child" },
        result: {
          run_id: "child",
          child_session_id: "child_session",
          status: "completed",
          output: '<img src=x onerror="alert(1)">',
          output_truncated: true,
          output_length: 9000,
          error: null,
        },
      },
    ],
    counts: { total: 1, running: 0, completed: 1 },
  });
  vi.spyOn(graphsApi, "list").mockResolvedValue(projection([value]));
  const first = mountPanel();
  await flushPromises();
  first.unmount();
  const second = mountPanel();
  await flushPromises();
  expect(second.find("pre").text()).toBe('<img src=x onerror="alert(1)">');
  expect(second.find("img").exists()).toBe(false);
  expect(second.text()).toContain("此处为结果节选");
  expect(second.text()).toContain("9000 字符");
  expect(second.findComponent({ name: "RouterLink" }).props("to")).toEqual({
    path: "/history",
    query: { session: "child_session", run: "child" },
  });
  expect(second.text()).toContain("1 / 1 个任务已完成");
  second.unmount();
});

it("clears old graphs immediately and ignores a previous run's late list response", async () => {
  let resolveOld!: (value: ReturnType<typeof projection>) => void;
  const list = vi
    .spyOn(graphsApi, "list")
    .mockResolvedValueOnce(projection([graph()]))
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    )
    .mockResolvedValueOnce(
      projection(
        [graph({ graph_id: "g2", root_run_id: "r2", title: "新运行方案" })],
        "r2",
      ),
    );
  const wrapper = mountPanel();
  await flushPromises();
  expect(wrapper.text()).toContain("调查登录失败");
  await button(wrapper, "刷新任务图").trigger("click");
  await wrapper.setProps({ runId: "r2" });
  expect(wrapper.text()).not.toContain("调查登录失败");
  await flushPromises();
  resolveOld(projection([graph({ title: "过期方案" })]));
  await flushPromises();
  expect(wrapper.text()).toContain("新运行方案");
  expect(wrapper.text()).not.toContain("过期方案");
  expect(list).toHaveBeenCalledTimes(3);
  wrapper.unmount();
});

it("a late save does not clear the new run's draft or add the old graph", async () => {
  vi.spyOn(graphsApi, "list").mockResolvedValue(projection([]));
  let resolveSave!: (value: AgentGraph) => void;
  vi.spyOn(graphsApi, "create").mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveSave = resolve;
      }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find('[aria-label="任务图名称"]').setValue("旧方案");
  await wrapper.find('[aria-label="task_1 的任务"]').setValue("旧调查");
  await wrapper.find("form").trigger("submit");
  await wrapper.setProps({ runId: "r2" });
  await wrapper.find('[aria-label="任务图名称"]').setValue("新草稿");
  resolveSave(graph());
  await flushPromises();
  expect(
    wrapper.find<HTMLInputElement>('[aria-label="任务图名称"]').element.value,
  ).toBe("新草稿");
  expect(wrapper.text()).not.toContain("方案已保存");
  expect(wrapper.text()).not.toContain("调查登录失败");
  wrapper.unmount();
});

it("does not poll unstarted plans; polls started waiting graphs and stops on unmount", async () => {
  vi.useFakeTimers();
  const list = vi
    .spyOn(graphsApi, "list")
    .mockResolvedValue(projection([graph()]));
  vi.spyOn(graphsApi, "advance").mockResolvedValue(graph({ started: true }));
  const wrapper = mountPanel();
  await flushPromises();
  await vi.advanceTimersByTimeAsync(6000);
  expect(list).toHaveBeenCalledTimes(1);
  await button(wrapper, "开始调查").trigger("click");
  await flushPromises();
  list.mockResolvedValue(projection([graph({ started: true })]));
  await vi.advanceTimersByTimeAsync(2000);
  expect(list).toHaveBeenCalledTimes(2);
  wrapper.unmount();
  await vi.advanceTimersByTimeAsync(6000);
  expect(list).toHaveBeenCalledTimes(2);
});

it("all graph mutations retain the same unknown request identity without storing task text", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(Response.json(graph()))
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(Response.json(graph({ started: true })))
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(Response.json(graph({ cancelled: true })));
  vi.stubGlobal("fetch", fetch);
  const definition = {
    title: "private plan",
    nodes: [
      {
        id: "a",
        kind: "readonly" as const,
        goal: "private task",
        depends_on: [],
      },
    ],
  };
  await expect(graphsApi.create("r1", definition)).rejects.toThrow("offline");
  expect(JSON.stringify({ ...sessionStorage })).not.toContain("private");
  await graphsApi.create("r1", definition);
  await expect(graphsApi.advance("g1")).rejects.toThrow("offline");
  await graphsApi.advance("g1");
  await expect(graphsApi.cancel("g1")).rejects.toThrow("offline");
  await graphsApi.cancel("g1");
  for (const [failed, retry] of [
    [0, 1],
    [2, 3],
    [4, 5],
  ]) {
    expect(fetch.mock.calls[retry]![1].headers["Idempotency-Key"]).toBe(
      fetch.mock.calls[failed]![1].headers["Idempotency-Key"],
    );
  }
  expect(fetch.mock.calls[0]![0]).toBe("/api/runs/r1/graphs");
  expect(fetch.mock.calls[2]![0]).toBe("/api/graphs/g1/advance");
  expect(fetch.mock.calls[4]![0]).toBe("/api/graphs/g1/cancel");
});

it("an old stop error cannot affect a different run", async () => {
  vi.spyOn(graphsApi, "list")
    .mockResolvedValueOnce(
      projection([graph({ started: true, state: "running" })]),
    )
    .mockResolvedValue(projection([], "r2"));
  let rejectStop!: (error: Error) => void;
  vi.spyOn(graphsApi, "cancel").mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        rejectStop = reject;
      }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  await button(wrapper, "停止任务图").trigger("click");
  await wrapper.setProps({ runId: "r2" });
  rejectStop(new Error("old stop failure"));
  await flushPromises();
  expect(wrapper.text()).not.toContain("old stop failure");
  expect(wrapper.text()).not.toContain("调查登录失败");
  wrapper.unmount();
});

it("stops polling when the saved graph has completed", async () => {
  vi.useFakeTimers();
  const list = vi
    .spyOn(graphsApi, "list")
    .mockResolvedValueOnce(
      projection([graph({ started: true, state: "running" })]),
    )
    .mockResolvedValue(
      projection([graph({ started: true, state: "completed" })]),
    );
  const wrapper = mountPanel();
  await flushPromises();
  await vi.advanceTimersByTimeAsync(2000);
  expect(list).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(6000);
  expect(list).toHaveBeenCalledTimes(2);
  wrapper.unmount();
});
