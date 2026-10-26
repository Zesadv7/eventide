import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createRouter, createMemoryHistory } from "vue-router";
import AttentionView from "../src/views/AttentionView.vue";
import { attentionApi, type AttentionSnapshot } from "../src/api/attention";
import { useWorkbench } from "../src/stores/workbench";
import { api } from "../src/api/client";

const snapshot: AttentionSnapshot = {
  generated_at: 1000,
  project_id: null,
  counts: { approvals: 2, runs: 1, delivery_candidates: 1, questions: 0 },
  questions: [],
  approvals: [
    {
      kind: "approval",
      dedup_key: "approval:approval_live_1",
      approval_id: "approval_live_1",
      run_id: "run-live",
      session_id: "session-live",
      workspace_id: "workspace-1",
      ts: 1000,
      tool: "bash",
      arguments: { command: "rm -rf build" },
      reason: "危险命令需要确认",
      summary: {
        tool: "bash",
        command: "rm -rf build",
        targets: [],
        query: null,
        cwd: "/repo",
        execution_kind: "source",
        execution_label: "源工作区（未隔离）",
        worktree_name: null,
        sandbox_id: null,
        impact: "写入源工作区（未隔离）",
        mcp_server: null,
        mcp_tool: null,
        network_access: null,
        privilege_elevation: null,
      },
      run_status: "waiting_for_user",
      actionable: true,
      action: {
        method: "POST",
        url: "/api/runs/run-live/approvals/approval_live_1",
      },
      links: {
        run: "/runs/run-live/diagnostics",
        session: "/chat/session-live",
      },
    },
    {
      kind: "approval",
      dedup_key: "approval:approval_stale_1",
      approval_id: "approval_stale_1",
      run_id: "run-stale",
      session_id: "session-stale",
      workspace_id: "workspace-1",
      ts: 900,
      tool: "write_file",
      arguments: { path: "src/a.py" },
      reason: null,
      summary: {
        tool: "write_file",
        command: null,
        targets: ["src/a.py"],
        query: null,
        cwd: null,
        execution_kind: "worktree",
        execution_label: "隔离工作副本",
        worktree_name: "wt-1",
        sandbox_id: null,
        impact: "写入隔离执行副本",
        mcp_server: null,
        mcp_tool: null,
        network_access: null,
        privilege_elevation: null,
      },
      run_status: "interrupted",
      actionable: false,
      action: {
        method: "POST",
        url: "/api/runs/run-stale/approvals/approval_stale_1",
      },
      links: {
        run: "/runs/run-stale/diagnostics",
        session: "/chat/session-stale",
      },
    },
  ],
  runs: [
    {
      kind: "run",
      dedup_key: "run:run-stale:interrupted",
      run_id: "run-stale",
      session_id: "session-stale",
      workspace_id: "workspace-1",
      ts: 950,
      status: "interrupted",
      error: null,
      reason: "用户停止",
      pending_approvals: 1,
      links: {
        run: "/runs/run-stale/diagnostics",
        session: "/chat/session-stale",
      },
    },
  ],
  delivery_candidates: [
    {
      kind: "delivery_candidate",
      dedup_key: "delivery:delivery-1",
      delivery_id: "delivery-1",
      name: "fix-login",
      workspace_id: "workspace-1",
      project_id: "project-1",
      patch_sha256: "a".repeat(64),
      links: { page: "/deliveries" },
    },
  ],
  unsupported: {},
};

beforeEach(() => {
  setActivePinia(createPinia());
});
afterEach(() => {
  vi.restoreAllMocks();
});

async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: AttentionView },
      { path: "/chat/:sessionId", component: { template: "<div>chat</div>" } },
      { path: "/deliveries", component: { template: "<div>deliveries</div>" } },
      {
        path: "/runs/:runId/diagnostics",
        component: { template: "<div>diagnostics</div>" },
      },
    ],
  });
  await router.push("/");
  await router.isReady();
  return {
    router,
    wrapper: mount(AttentionView, { global: { plugins: [router] } }),
  };
}

describe("attention center", () => {
  it("renders readable approval facts and hides stale actions", async () => {
    vi.spyOn(attentionApi, "global").mockResolvedValue(snapshot);
    const { wrapper } = await render();
    await flushPromises();
    expect(wrapper.text()).toContain("rm -rf build");
    expect(wrapper.text()).toContain("写入源工作区（未隔离）");
    expect(wrapper.text()).toContain("网络访问未声明");
    expect(wrapper.text()).toContain("展开原始 JSON");
    const stale = wrapper
      .findAll(".attention-item")
      .find((node) => node.text().includes("approval_stale_1"));
    expect(stale).toBeTruthy();
    expect(stale!.text()).toContain("已失效");
    const staleButtons = stale!.findAll("button");
    expect(
      staleButtons.filter((button) => button.text() === "批准")[0].attributes(),
    ).toHaveProperty("disabled");
    // The original JSON stays expandable and untouched by the display layer.
    const live = wrapper
      .findAll(".attention-item")
      .find((node) => node.text().includes("approval_live_1"))!;
    await live
      .findAll("button")
      .filter((b) => b.text().includes("JSON"))[0]
      .trigger("click");
    expect(live.text()).toContain("rm -rf build");
    expect(wrapper.text()).toContain("当前没有待回答的问题");
  });

  it("reuses the original approval endpoint and surfaces rejection", async () => {
    vi.spyOn(attentionApi, "global").mockResolvedValue(snapshot);
    const approve = vi
      .spyOn(api, "approve")
      .mockRejectedValue(
        new (await import("../src/api/client")).ApiError(409, "conflict"),
      );
    const { wrapper } = await render();
    await flushPromises();
    const live = wrapper
      .findAll(".attention-item")
      .find((node) => node.text().includes("approval_live_1"))!;
    await live
      .findAll("button")
      .filter((button) => button.text() === "批准")[0]
      .trigger("click");
    await flushPromises();
    expect(approve).toHaveBeenCalledWith("run-live", "approval_live_1", true);
    expect(live.text()).toContain("审批已失效或运行已结束");
  });

  it("shows empty sections without fabricating categories", async () => {
    vi.spyOn(attentionApi, "global").mockResolvedValue({
      ...snapshot,
      counts: { approvals: 0, runs: 0, delivery_candidates: 0, questions: 0 },
      approvals: [],
      runs: [],
      delivery_candidates: [],
    });
    const { wrapper } = await render();
    await flushPromises();
    expect(wrapper.text()).toContain("当前没有待审批请求");
    expect(wrapper.text()).toContain("当前没有失败或停驻的运行");
    expect(wrapper.text()).toContain("当前没有通过验收、等待人工采纳的候选");
  });
});

describe("attention project scope", () => {
  beforeEach(() => {
    useWorkbench().workspaces = [
      {
        workspace_id: "workspace-1",
        project_id: "project-1",
        name: "项目甲",
        path: "/a",
      },
      {
        workspace_id: "workspace-2",
        project_id: "project-2",
        name: "项目乙",
        path: "/b",
      },
    ];
  });

  it("keeps the scope in the URL and ignores a late global response", async () => {
    let finish!: (value: AttentionSnapshot) => void;
    vi.spyOn(attentionApi, "global").mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const project = vi.spyOn(attentionApi, "project").mockResolvedValue({
      ...snapshot,
      project_id: "project-1",
      approvals: [],
    });
    const { wrapper, router } = await render();
    await wrapper
      .get('select[aria-label="待处理项目范围"]')
      .setValue("project-1");
    await flushPromises();
    expect(router.currentRoute.value.query.project).toBe("project-1");
    expect(project).toHaveBeenCalledWith("project-1");
    expect(wrapper.text()).not.toContain("rm -rf build");
    finish(snapshot);
    await flushPromises();
    expect(wrapper.text()).not.toContain("rm -rf build");
    wrapper.unmount();
    const reopened = mount(AttentionView, { global: { plugins: [router] } });
    await flushPromises();
    expect((reopened.get("select").element as HTMLSelectElement).value).toBe(
      "project-1",
    );
    expect(project).toHaveBeenCalledTimes(2);
    reopened.unmount();
  });

  it("clears the previous project's actions immediately and does not fall back on errors", async () => {
    vi.spyOn(attentionApi, "global").mockResolvedValue(snapshot);
    let reject!: (reason: Error) => void;
    vi.spyOn(attentionApi, "project").mockImplementation(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    const { wrapper } = await render();
    await flushPromises();
    expect(wrapper.text()).toContain("rm -rf build");
    await wrapper.get("select").setValue("project-2");
    await flushPromises();
    expect(wrapper.text()).not.toContain("rm -rf build");
    reject(new Error("项目已不可用"));
    await flushPromises();
    expect(wrapper.text()).toContain("项目已不可用");
    expect(wrapper.text()).not.toContain("正在读取");
    expect(wrapper.text()).not.toContain("rm -rf build");
    expect(attentionApi.global).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("does not report an old approval failure in the newly selected project", async () => {
    vi.spyOn(attentionApi, "global").mockResolvedValue(snapshot);
    const project = vi.spyOn(attentionApi, "project").mockResolvedValue({
      ...snapshot,
      project_id: "project-2",
      approvals: [],
    });
    let reject!: (reason: Error) => void;
    vi.spyOn(api, "approve").mockImplementation(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    const { wrapper } = await render();
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "批准")!
      .trigger("click");
    await wrapper.get("select").setValue("project-2");
    await flushPromises();
    reject(new Error("旧项目审批失败"));
    await flushPromises();
    expect(wrapper.text()).not.toContain("旧项目审批失败");
    expect(project).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });
});
