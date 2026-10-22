import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  indexesApi,
  indexRequestState,
  type IndexDetail,
} from "../src/api/indexes";
import IndexesView from "../src/views/IndexesView.vue";
import { useWorkbench } from "../src/stores/workbench";
const config = {
  provider: "hash",
  model: "hash-v1",
  algorithm_version: "hash-sha256-v1",
  dimension: 256,
  config_hash: "a".repeat(64),
};
const input = {
  config_hash: config.config_hash,
  force: false,
  code_structure: false,
};
let project = "",
  sequence = 0;
const detail = (
  id = "index-one",
  extra: Partial<IndexDetail> = {},
): IndexDetail => ({
  run_id: id,
  session_id: "index-session",
  project_id: project,
  status: "completed",
  configuration: config,
  force: false,
  code_structure: false,
  published: {
    generation: 2,
    stats: {
      files: 3,
      chunks: 5,
      rebuilt_files: 1,
      deleted_files: 0,
      skipped: 0,
      duration_ms: 10,
      structured_files: 0,
      structure_fallbacks: {},
      chunking_fingerprint: "b".repeat(64),
      generation: 2,
    },
  },
  error: null,
  events_url: `/api/runs/${id}/events`,
  ...extra,
});
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  project = `index-project-${++sequence}`;
  const store = useWorkbench();
  store.workspaces = [
    {
      workspace_id: "w",
      project_id: project,
      name: "当前项目",
      path: "/fixture",
    },
    {
      workspace_id: "other",
      project_id: "other-project",
      name: "其他项目",
      path: "/other",
    },
  ];
  store.selectedWorkspace = "w";
  vi.spyOn(indexesApi, "status").mockImplementation(async (workspace_id) => ({
    status: "available",
    workspace_id,
    generation: 2,
    location: "managed",
  }));
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render(query = "") {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/knowledge", component: IndexesView },
      { path: "/usage", component: { template: "<p>usage</p>" } },
    ],
  });
  await router.push(`/knowledge${query}`);
  await router.isReady();
  const wrapper = mount(IndexesView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}
function reads() {
  vi.spyOn(indexesApi, "configuration").mockResolvedValue(config);
  vi.spyOn(indexesApi, "list").mockResolvedValue([detail()]);
  return vi
    .spyOn(indexesApi, "detail")
    .mockImplementation(async (id) => detail(id));
}
it("restores a historical URL outside the first page and links exact usage", async () => {
  reads();
  const { wrapper } = await render("?index=old-index");
  expect(wrapper.text()).toContain("发布代次 2");
  expect(wrapper.find('a[href="/usage?run=old-index"]').exists()).toBe(true);
  expect(wrapper.text()).toContain("Hash 仅验证检索机制");
  wrapper.unmount();
});
it("uncertain admission retains exact identity and body across retry", async () => {
  const keys: string[] = [];
  const receipt = {
    run_id: "accepted",
    session_id: "control",
    project_id: project,
    status_url: "/status",
    events_url: "/events",
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url, options) => {
      keys.push(options.headers["Idempotency-Key"]);
      if (keys.length === 1) throw new Error("lost response");
      return new Response(JSON.stringify(receipt), { status: 202 });
    }),
  );
  await expect(indexesApi.create(project, input)).rejects.toThrow(
    "lost response",
  );
  expect(indexRequestState(project).pending).toEqual(input);
  await expect(
    indexesApi.create(project, { ...input, force: true }),
  ).rejects.toThrow("原索引请求");
  expect(await indexesApi.create(project, input)).toEqual(receipt);
  expect(keys[0]).toBe(keys[1]);
  expect(indexRequestState(project).pending).toBeNull();
});
it("pending controls show the frozen options after reload", async () => {
  reads();
  sessionStorage.setItem(
    `eventide.index-pending.v1.${project}`,
    JSON.stringify({ ...input, force: true, code_structure: true }),
  );
  const { wrapper } = await render();
  const boxes = wrapper.findAll('input[type="checkbox"]');
  expect((boxes[0].element as HTMLInputElement).checked).toBe(true);
  expect(boxes[0].attributes("disabled")).toBeDefined();
  expect(wrapper.text()).toContain("重试原索引请求");
  wrapper.unmount();
});
it("late detail from a previous project cannot replace the new view", async () => {
  const read = reads();
  let finish!: (v: IndexDetail) => void;
  read.mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const { wrapper } = await render("?index=old");
  useWorkbench().selectedWorkspace = "other";
  await flushPromises();
  finish(detail("old"));
  await flushPromises();
  expect(wrapper.text()).not.toContain("发布代次 2");
  expect(wrapper.text()).toContain("属于其他项目");
  wrapper.unmount();
});
it("exact stop updates status without cancelling an Agent conversation", async () => {
  const read = reads();
  read.mockResolvedValue(
    detail("index-one", { status: "running", published: null }),
  );
  const cancel = vi
    .spyOn(indexesApi, "cancel")
    .mockResolvedValue(
      detail("index-one", { status: "interrupted", published: null }),
    );
  const { wrapper } = await render();
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "停止构建")!
    .trigger("click");
  await flushPromises();
  expect(cancel).toHaveBeenCalledWith("index-one");
  expect(wrapper.text()).toContain("已中断");
  wrapper.unmount();
});

it("keeps closed index details closed across polling and a late response without stopping the task", async () => {
  vi.useFakeTimers();
  vi.spyOn(indexesApi, "configuration").mockResolvedValue(config);
  vi.spyOn(indexesApi, "list").mockResolvedValue([detail()]);
  let finish!: (value: IndexDetail) => void;
  const read = vi.spyOn(indexesApi, "detail").mockResolvedValue(detail());
  const cancel = vi.spyOn(indexesApi, "cancel");
  const { wrapper } = await render();
  await flushPromises();
  expect(wrapper.find("article").exists()).toBe(true);
  read.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  // An explicit refresh starts a read while the panel is still visible.
  await vi.advanceTimersByTimeAsync(3000);
  await wrapper.get('[aria-label="关闭详情"]').trigger("click");
  await flushPromises();
  finish?.(detail());
  await flushPromises();
  await vi.advanceTimersByTimeAsync(3000);
  await flushPromises();
  expect(wrapper.find("article").exists()).toBe(false);
  expect(cancel).not.toHaveBeenCalled();
  wrapper.unmount();
  vi.useRealTimers();
});

it("chooses only project workspaces and keeps the build target in the request", async () => {
  reads();
  useWorkbench().workspaces.push({
    workspace_id: "writer",
    project_id: project,
    kind: "worktree",
    name: "Writer",
    path: "/writer",
  });
  const create = vi
    .spyOn(indexesApi, "create")
    .mockRejectedValue(new Error("fixture rejected"));
  const { wrapper } = await render();
  const select = wrapper.get('[aria-label="索引目标"]');
  expect((select.element as HTMLSelectElement).value).toBe("w");
  expect(select.findAll("option")).toHaveLength(2);
  await select.setValue("writer");
  await flushPromises();
  expect(wrapper.get('[aria-label="知识检索状态"]').text()).toContain("writer");
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "建立索引")!
    .trigger("click");
  await flushPromises();
  expect(create).toHaveBeenCalledWith(project, {
    ...input,
    workspace_id: "writer",
  });
  wrapper.unmount();
});
it("restores the exact workspace of an uncertain build and rejects changing it", async () => {
  reads();
  useWorkbench().workspaces.push({
    workspace_id: "writer",
    project_id: project,
    kind: "worktree",
    name: "Writer",
    path: "/writer",
  });
  const body = { ...input, workspace_id: "writer" };
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("lost response")));
  await expect(indexesApi.create(project, body)).rejects.toThrow(
    "lost response",
  );
  expect(indexRequestState(project).pending).toEqual(body);
  await expect(
    indexesApi.create(project, { ...body, workspace_id: "w" }),
  ).rejects.toThrow("原索引请求");
  const { wrapper } = await render();
  expect(
    (wrapper.get('[aria-label="索引目标"]').element as HTMLSelectElement).value,
  ).toBe("writer");
  expect(
    wrapper.get('[aria-label="索引目标"]').attributes("disabled"),
  ).toBeDefined();
  wrapper.unmount();
});
it("ignores a late status for another workspace and reports legacy location without freshness claims", async () => {
  reads();
  useWorkbench().workspaces.push({
    workspace_id: "writer",
    project_id: project,
    kind: "worktree",
    name: "Writer",
    path: "/writer",
  });
  const resolvers: ((
    v: Awaited<ReturnType<typeof indexesApi.status>>,
  ) => void)[] = [];
  vi.mocked(indexesApi.status).mockImplementation((workspace_id) =>
    workspace_id === "w"
      ? new Promise((resolve) => resolvers.push(resolve))
      : Promise.resolve({
          status: "available",
          workspace_id,
          location: "legacy",
          generation: 9,
        }),
  );
  const { wrapper } = await render();
  await wrapper.get('[aria-label="索引目标"]').setValue("writer");
  await flushPromises();
  for (const resolve of resolvers)
    resolve({ status: "failed", workspace_id: "w", reason: "stale response" });
  await flushPromises();
  expect(wrapper.get('[aria-label="知识检索状态"]').text()).toContain(
    "旧位置（只读）",
  );
  expect(wrapper.get('[aria-label="知识检索状态"]').text()).toContain(
    "不代表全部源码最新",
  );
  expect(wrapper.text()).not.toContain("stale response");
  wrapper.unmount();
});
