import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  memoryApi,
  memoryPending,
  type MemoryRecord,
  type MemoryState,
} from "../src/api/memory";
import MemoryView from "../src/views/MemoryView.vue";
import RunMemoryPanel from "../src/components/RunMemoryPanel.vue";
import { useWorkbench } from "../src/stores/workbench";
import { ApiError } from "../src/api/client";
const record = (extra: Partial<MemoryRecord> = {}): MemoryRecord => ({
  id: "mem_one",
  revision: 2,
  scope: "global",
  project_id: null,
  key: "language",
  content: "用中文回答",
  status: "active",
  source: { type: "explicit", session_id: "s1", event_ids: ["evt1"] },
  created_at: 1,
  updated_at: 2,
  ...extra,
});
const state = (extra: Partial<MemoryState> = {}): MemoryState => ({
  settings: { enabled: true, revision: 1 },
  memories: [
    record(),
    record({
      id: "project_one",
      scope: "project",
      project_id: "p1",
      key: "style",
      content: "保留注释",
    }),
  ],
  suggestions: [
    {
      ...record({ id: "suggestion_one", key: "brevity", content: "简短回答" }),
      status: "pending",
    },
  ],
  warning: "撤销不会擦除历史",
  ...extra,
});
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  const store = useWorkbench();
  store.workspaces = [
    { workspace_id: "w1", project_id: "p1", name: "项目一", path: "/one" },
    { workspace_id: "w2", project_id: "p2", name: "项目二", path: "/two" },
  ];
  store.selectedWorkspace = "w1";
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});
async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/memory", component: MemoryView },
      {
        path: "/chat/:sessionId?",
        name: "chat",
        component: { template: "<p>chat</p>" },
      },
    ],
  });
  await router.push("/memory");
  await router.isReady();
  const wrapper = mount(MemoryView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}
function reads(value = state()) {
  return vi.spyOn(memoryApi, "list").mockResolvedValue(value);
}
function response(status = 200, body: unknown = {}, rejected = false) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...(rejected ? { "X-Eventide-Mutation-Outcome": "rejected" } : {}),
    },
  });
}
function button(wrapper: ReturnType<typeof mount>, text: string) {
  return wrapper.findAll("button").find((b) => b.text() === text)!;
}
it("lists exact selected scope, searches locally and links real source session", async () => {
  const list = reads();
  const { wrapper } = await render();
  expect(list).toHaveBeenCalledWith("p1");
  expect(wrapper.text()).toContain("全局仅指当前 Eventide");
  expect(wrapper.text()).toContain("用中文回答");
  expect(wrapper.text()).not.toContain("保留注释");
  expect(wrapper.find('a[href="/chat/s1"]').exists()).toBe(true);
  expect(wrapper.text()).toContain("evt1");
  await wrapper.find("select").setValue("project");
  expect(wrapper.text()).toContain("保留注释");
  expect(wrapper.find('section[aria-label="已保存偏好"]').text()).not.toContain(
    "用中文回答",
  );
  await wrapper.find('input[type="search"]').setValue("没有");
  expect(wrapper.text()).toContain("没有匹配的偏好");
  wrapper.unmount();
});
it("confirms candidates with their precise revision, never enables on read", async () => {
  reads();
  const resolve = vi.spyOn(memoryApi, "resolve").mockResolvedValue({});
  const { wrapper } = await render();
  expect(resolve).not.toHaveBeenCalled();
  await button(wrapper, "确认保存").trigger("click");
  await flushPromises();
  expect(resolve).toHaveBeenCalledWith("suggestion_one", 2, "accept");
  expect(wrapper.text()).toContain("下次 Run 生效");
  wrapper.unmount();
});
it("saves scoped manual preferences and reports next Run timing", async () => {
  reads();
  const create = vi.spyOn(memoryApi, "create").mockResolvedValue({});
  const { wrapper } = await render();
  await wrapper.find("select").setValue("project");
  await button(wrapper, "新增偏好").trigger("click");
  const form = wrapper.find(".memory-editor");
  await form.find("input").setValue("format");
  await form.find("textarea").setValue("使用短段落");
  await form.trigger("submit");
  await flushPromises();
  expect(create).toHaveBeenCalledWith({
    scope: "project",
    project_id: "p1",
    key: "format",
    content: "使用短段落",
  });
  expect(wrapper.text()).toContain("新增或普通编辑将在下次 Run 生效");
  wrapper.unmount();
});
it("guards unsaved scope and route changes, retaining draft on cancel", async () => {
  reads();
  const { wrapper, router } = await render();
  await button(wrapper, "新增偏好").trigger("click");
  await wrapper.find("textarea").setValue("未保存内容");
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  await wrapper.find("select").setValue("project");
  expect(wrapper.find("select").element.value).toBe("global");
  await router.push("/chat/s1");
  expect(router.currentRoute.value.path).toBe("/memory");
  expect(confirm).toHaveBeenCalled();
  expect(wrapper.find("textarea").element.value).toBe("未保存内容");
  wrapper.unmount();
});
it("keeps conflicting edit draft without silently advancing its expected revision", async () => {
  const list = reads();
  const update = vi
    .spyOn(memoryApi, "update")
    .mockRejectedValue(new ApiError(409, "conflict", "rejected"));
  const { wrapper } = await render();
  await button(wrapper, "编辑").trigger("click");
  await wrapper.find("textarea").setValue("我的未保存修改");
  list.mockResolvedValue(
    state({ memories: [record({ revision: 3, content: "其他标签页修改" })] }),
  );
  await wrapper.find(".memory-editor").trigger("submit");
  await flushPromises();
  expect(update).toHaveBeenCalledWith("mem_one", 2, {
    content: "我的未保存修改",
  });
  expect(wrapper.text()).toContain("不会覆盖较新版本");
  expect(wrapper.find("textarea").element.value).toBe("我的未保存修改");
  expect(wrapper.text()).toContain("其他标签页修改");
  wrapper.unmount();
});
it("requires explicit revoke and global off confirmation with historical warning", async () => {
  reads();
  const revoke = vi.spyOn(memoryApi, "revoke").mockResolvedValue({});
  const settings = vi.spyOn(memoryApi, "settings").mockResolvedValue({});
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  const { wrapper } = await render();
  await button(wrapper, "撤销").trigger("click");
  expect(revoke).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0]?.[0]).toContain("不会擦除历史");
  confirm.mockReturnValue(true);
  await button(wrapper, "撤销").trigger("click");
  await flushPromises();
  expect(revoke).toHaveBeenCalledWith("mem_one", 2);
  await button(wrapper, "关闭偏好记忆").trigger("click");
  await flushPromises();
  expect(settings).toHaveBeenCalledWith(false, 1);
  wrapper.unmount();
});
it("ignores late prior-project response", async () => {
  let resolve!: (value: MemoryState) => void;
  vi.spyOn(memoryApi, "list").mockImplementation((project) =>
    project === "p1"
      ? new Promise((r) => {
          resolve = r;
        })
      : Promise.resolve(
          state({ memories: [record({ content: "项目二数据" })] }),
        ),
  );
  const { wrapper } = await render();
  useWorkbench().selectedWorkspace = "w2";
  await flushPromises();
  resolve(state({ memories: [record({ content: "项目一迟到数据" })] }));
  await flushPromises();
  expect(wrapper.text()).toContain("项目二数据");
  expect(wrapper.text()).not.toContain("项目一迟到数据");
  wrapper.unmount();
});
it("unknown write retains identity, rejects different intent and persists no preference content", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(response(503))
    .mockResolvedValue(response(200, { memory: record() }));
  vi.stubGlobal("fetch", fetch);
  const body = {
    scope: "global" as const,
    project_id: null,
    key: "language",
    content: "保密偏好正文",
  };
  await expect(memoryApi.create(body)).rejects.toThrow();
  expect(memoryPending()).not.toBeNull();
  expect(JSON.stringify(sessionStorage)).not.toContain("保密偏好正文");
  expect(JSON.stringify(sessionStorage)).not.toContain("language");
  await expect(
    memoryApi.create({ ...body, content: "另一意图" }),
  ).rejects.toThrow("结果未知");
  expect(fetch).toHaveBeenCalledTimes(1);
  await memoryApi.retry();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][1].headers["Idempotency-Key"]).toBe(
    fetch.mock.calls[1][1].headers["Idempotency-Key"],
  );
  expect(memoryPending()).toBeNull();
});
it("reconstructs unknown content edit with original revision after refresh without storing its text", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(response(503))
    .mockResolvedValue(response());
  vi.stubGlobal("fetch", fetch);
  await expect(
    memoryApi.update("mem_one", 2, { content: "原偏好" }),
  ).rejects.toThrow();
  await expect(memoryApi.recover("不同内容", "")).rejects.toThrow("结果未知");
  await memoryApi.recover("原偏好", "");
  expect(fetch.mock.calls[1][1].body).toBe(fetch.mock.calls[0][1].body);
  expect(fetch.mock.calls[1][1].headers["Idempotency-Key"]).toBe(
    fetch.mock.calls[0][1].headers["Idempotency-Key"],
  );
  expect(memoryPending()).toBeNull();
});
it("reconstructs non-content unknown operation and clears only marked rejection", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(response(409))
    .mockResolvedValueOnce(response(409, {}, true))
    .mockResolvedValue(response());
  vi.stubGlobal("fetch", fetch);
  await expect(memoryApi.settings(false, 1)).rejects.toThrow();
  expect(memoryPending()).not.toBeNull();
  await expect(memoryApi.recover("", "")).rejects.toThrow();
  expect(memoryPending()).toBeNull();
  await memoryApi.settings(false, 2);
  expect(fetch.mock.calls[0][1].headers["Idempotency-Key"]).not.toBe(
    fetch.mock.calls[2][1].headers["Idempotency-Key"],
  );
});
it("historical panel distinguishes snapshot, request evidence and current status", async () => {
  vi.spyOn(memoryApi, "run").mockResolvedValue({
    snapshotted: true,
    snapshot: {
      memories: [
        {
          id: "old",
          revision: 1,
          scope: "global",
          project_id: null,
          key: "old_topic",
        },
      ],
    },
    injections: [
      {
        request_id: "request1",
        attempt_id: "attempt1",
        memories: [
          {
            id: "old",
            revision: 1,
            scope: "global",
            project_id: null,
            key: "old_topic",
          },
        ],
        digest: "x",
      },
    ],
    receipts: [{ type: "memory.saved", memory_id: "new" }],
  });
  const wrapper = mount(RunMemoryPanel, {
    props: { runId: "r1" },
    global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
  });
  await flushPromises();
  expect(wrapper.text()).toContain("历史运行证据，不代表当前启用状态");
  expect(wrapper.text()).toContain("运行开始时固定");
  expect(wrapper.text()).toContain("请求 request1");
  expect(wrapper.text()).toContain("下次 Run 生效");
  expect(wrapper.text()).not.toContain("模型已遵循");
  wrapper.unmount();
});
it("historical panel discards late response when run changes", async () => {
  let resolve!: (value: any) => void;
  vi.spyOn(memoryApi, "run").mockImplementation((id) =>
    id === "r1"
      ? new Promise((r) => (resolve = r))
      : Promise.resolve({ snapshotted: false, injections: [], receipts: [] }),
  );
  const wrapper = mount(RunMemoryPanel, {
    props: { runId: "r1" },
    global: { stubs: { RouterLink: true } },
  });
  await wrapper.setProps({ runId: "r2" });
  await flushPromises();
  resolve({
    snapshotted: true,
    injections: [],
    receipts: [{ type: "memory.rejected", reason: "旧Run迟到" }],
  });
  await flushPromises();
  expect(wrapper.text()).not.toContain("旧Run迟到");
  expect(wrapper.text()).toContain("尚无偏好快照");
  wrapper.unmount();
});

it("provides accessible top close for new/edit forms and guards dirty drafts", async () => {
  reads();
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  const { wrapper } = await render();
  await button(wrapper, "新增偏好").trigger("click");
  await wrapper.find('button[aria-label="关闭偏好编辑"]').trigger("click");
  expect(wrapper.find(".memory-editor").exists()).toBe(false);
  expect(confirm).not.toHaveBeenCalled();
  await button(wrapper, "编辑").trigger("click");
  await wrapper.find("textarea").setValue("未保存偏好");
  await wrapper.find('button[aria-label="关闭偏好编辑"]').trigger("click");
  expect(confirm).toHaveBeenCalled();
  expect(wrapper.find("textarea").element.value).toBe("未保存偏好");
  confirm.mockReturnValue(true);
  await wrapper.find('button[aria-label="关闭偏好编辑"]').trigger("click");
  expect(wrapper.find(".memory-editor").exists()).toBe(false);
  wrapper.unmount();
});
