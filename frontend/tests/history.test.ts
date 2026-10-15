import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  historyApi,
  type HistoryRun,
  type HistorySession,
} from "../src/api/history";
import HistoryView from "../src/views/HistoryView.vue";
import { useWorkbench } from "../src/stores/workbench";

const archived: HistorySession = {
  session_id: "archived-session",
  workspace_id: "workspace",
  project_id: "project",
  title: "历史归档会话",
  working_directory: "/fixture",
  archived: true,
  status: "completed",
  latest_run: null,
};
const live: HistorySession = {
  ...archived,
  session_id: "live-session",
  title: "正在工作的会话",
  archived: false,
};
const value = (index: number, extra: Partial<HistoryRun> = {}): HistoryRun => ({
  run_id: `run-${index}`,
  session_id: archived.session_id,
  status: "completed",
  output: `历史输出 ${index}`,
  started_at: 1,
  steps: 0,
  tool_calls: 0,
  ...extra,
});
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
  const store = useWorkbench();
  store.workspaces = [
    {
      workspace_id: "workspace",
      project_id: "project",
      name: "项目一",
      path: "/fixture",
    },
    {
      workspace_id: "other-workspace",
      project_id: "other-project",
      name: "项目二",
      path: "/other",
    },
  ];
  store.selectedWorkspace = "workspace";
  store.selectedSession = live.session_id;
  store.runs[live.session_id] = [
    {
      ...value(999, { session_id: live.session_id, status: "running" }),
      execution: undefined,
    },
  ];
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
function mockReads(page = [value(1)]) {
  vi.spyOn(historyApi, "sessions").mockResolvedValue([live, archived]);
  const runs = vi.spyOn(historyApi, "runs").mockResolvedValue(page);
  const run = vi
    .spyOn(historyApi, "run")
    .mockImplementation(async (id) => value(Number(id.split("-").at(-1))));
  return { runs, run };
}
async function render(query = "?session=archived-session") {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/history", component: HistoryView },
      { path: "/evaluations", component: { template: "<p>evaluation</p>" } },
    ],
  });
  await router.push(`/history${query}`);
  await router.isReady();
  const wrapper = mount(HistoryView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}

describe("history transport and selection", () => {
  it("uses the project archive catalog and backend identity cursor without changing page order", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json([archived]))
      .mockResolvedValueOnce(
        Response.json([
          { id: "old-1", session_id: "session/a", status: "completed" },
          { id: "old-2", session_id: "session/a", status: "completed" },
        ]),
      );
    vi.stubGlobal("fetch", fetch);
    await historyApi.sessions("project/a");
    const runs = await historyApi.runs("session/a", "cursor/a");
    expect(fetch.mock.calls[0]![0]).toBe(
      "/api/projects/project%2Fa/sessions?include_archived=true",
    );
    expect(fetch.mock.calls[1]![0]).toBe(
      "/api/sessions/session%2Fa/runs?limit=100&before=cursor%2Fa",
    );
    expect(runs.map((run) => run.run_id)).toEqual(["old-1", "old-2"]);
    expect(historyApi.exportUrl("run/a")).toBe("/api/runs/run%2Fa/export");
  });
  it("loads beyond 100 using the oldest identity, preserves chronology and leaves live selection alone", async () => {
    const page = Array.from({ length: 100 }, (_, index) => value(index + 5));
    const { runs } = mockReads(page);
    runs.mockImplementation(async (_owner, before) =>
      before ? Array.from({ length: 5 }, (_, index) => value(index)) : page,
    );
    const store = useWorkbench(),
      before = JSON.stringify(store.runs);
    const { wrapper, router } = await render();
    expect(wrapper.text()).toContain("已归档");
    expect(wrapper.findAll(".run-history-list button")).toHaveLength(100);
    expect(wrapper.findAll(".run-history-list small")[0]!.text()).toBe(
      "run-104",
    );
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "加载更早运行")!
      .trigger("click");
    await flushPromises();
    expect(runs).toHaveBeenLastCalledWith("archived-session", "run-5");
    expect(wrapper.findAll(".run-history-list button")).toHaveLength(105);
    expect(wrapper.findAll(".run-history-list small").at(-1)!.text()).toBe(
      "run-0",
    );
    await wrapper.findAll(".run-history-list button").at(-1)!.trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.query).toEqual({
      session: "archived-session",
      run: "run-0",
    });
    expect(wrapper.get('[aria-label="历史运行详情"]').text()).toContain(
      "历史输出 0",
    );
    expect(wrapper.get("a[download]").attributes("href")).toBe(
      "/api/runs/run-0/export",
    );
    expect(store.selectedSession).toBe("live-session");
    expect(store.currentRun?.run_id).toBe("run-999");
    expect(JSON.stringify(store.runs)).toBe(before);
    wrapper.unmount();
  });
  it("restores an older URL Run directly without downloading all earlier pages", async () => {
    const { runs, run } = mockReads(
      Array.from({ length: 100 }, (_, index) => value(index + 5)),
    );
    const { wrapper } = await render("?session=archived-session&run=run-0");
    expect(runs).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith("run-0");
    expect(wrapper.get('[aria-label="历史运行详情"]').text()).toContain(
      "历史输出 0",
    );
    wrapper.unmount();
  });
  it("never fetches a URL Session outside the current project", async () => {
    const { runs, run } = mockReads();
    const { wrapper } = await render("?session=foreign&run=run-0");
    expect(wrapper.text()).toContain("不在当前项目");
    expect(runs).not.toHaveBeenCalled();
    expect(run).not.toHaveBeenCalled();
    expect(wrapper.find("a[download]").exists()).toBe(false);
    wrapper.unmount();
  });
  it("rejects a Run from another Session and removes the export action", async () => {
    mockReads();
    vi.spyOn(historyApi, "run").mockResolvedValue(
      value(0, { session_id: "foreign-session" }),
    );
    const { wrapper } = await render("?session=archived-session&run=run-0");
    expect(wrapper.text()).toContain("不属于所选会话");
    expect(wrapper.find("a[download]").exists()).toBe(false);
    wrapper.unmount();
  });
  it("drops late older pages and details after a project switch", async () => {
    const older = deferred<HistoryRun[]>(),
      oldDetail = deferred<HistoryRun>();
    mockReads(Array.from({ length: 100 }, (_, index) => value(index)));
    const other = {
      ...archived,
      project_id: "other-project",
      session_id: "other-session",
      title: "第二项目会话",
    };
    vi.spyOn(historyApi, "sessions").mockImplementation(async (owner) =>
      owner === "project" ? [archived] : [other],
    );
    vi.spyOn(historyApi, "runs").mockImplementation(async (owner, before) =>
      owner === "other-session"
        ? [value(200, { session_id: owner })]
        : before
          ? older.promise
          : Array.from({ length: 100 }, (_, index) => value(index)),
    );
    vi.spyOn(historyApi, "run").mockImplementation((id) =>
      id === "run-200"
        ? Promise.resolve(value(200, { session_id: "other-session" }))
        : oldDetail.promise,
    );
    const { wrapper } = await render();
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "加载更早运行")!
      .trigger("click");
    useWorkbench().selectedWorkspace = "other-workspace";
    await flushPromises();
    oldDetail.resolve(value(99));
    older.resolve([value(-1)]);
    await flushPromises();
    expect(
      wrapper.findAll(".run-history-list small").map((node) => node.text()),
    ).toEqual(["run-200"]);
    expect(wrapper.get('[aria-label="历史运行详情"]').text()).toContain(
      "历史输出 200",
    );
    wrapper.unmount();
  });
  it("drops a late previous Run detail without replacing the selected one", async () => {
    const delayed = deferred<HistoryRun>();
    mockReads([value(0), value(1)]);
    vi.spyOn(historyApi, "run").mockImplementation((id) =>
      id === "run-1" ? delayed.promise : Promise.resolve(value(0)),
    );
    const { wrapper } = await render();
    await wrapper.findAll(".run-history-list button").at(-1)!.trigger("click");
    await flushPromises();
    delayed.resolve(value(1));
    await flushPromises();
    expect(wrapper.get('[aria-label="历史运行详情"]').text()).toContain(
      "历史输出 0",
    );
    wrapper.unmount();
  });
  it("shows tool evidence and an evaluation link without claiming validation from completion", async () => {
    mockReads();
    vi.spyOn(historyApi, "run").mockResolvedValue(
      value(1, {
        execution: {
          kind: "evaluation",
          workspace_id: "workspace",
          evaluation_id: "run-1",
        },
        tools: {
          call1: {
            name: "read_file",
            status: "completed",
            output: "<script>raw text</script>",
            is_error: false,
          },
        },
      }),
    );
    const { wrapper } = await render();
    expect(
      wrapper.get('a[href="/evaluations?evaluation=run-1"]').text(),
    ).toContain("评测");
    expect(wrapper.findAll(".tool-evidence")).toHaveLength(1);
    expect(wrapper.get(".tool-evidence pre").text()).toContain(
      "<script>raw text</script>",
    );
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.text()).toContain("运行完成不等于变更已验收");
    wrapper.unmount();
  });
  it("does not starve a slow active Run read on polling ticks and stops on unmount", async () => {
    vi.useFakeTimers();
    const active = value(1, { status: "running" });
    mockReads([active]);
    const read = vi
      .spyOn(historyApi, "run")
      .mockResolvedValueOnce(active)
      .mockImplementation(
        () =>
          new Promise((resolve) =>
            setTimeout(() => resolve({ ...active, status: "completed" }), 5000),
          ),
      );
    const { wrapper } = await render();
    await vi.advanceTimersByTimeAsync(6000);
    expect(read).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(wrapper.get('[aria-label="历史运行详情"]').text()).toContain(
      "已完成",
    );
    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(10000);
    expect(read).toHaveBeenCalledTimes(2);
  });
});
