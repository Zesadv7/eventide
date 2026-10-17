import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  sessionsApi,
  sessionMutationState,
  type SessionIntent,
} from "../src/api/sessions";
import { api, ApiError } from "../src/api/client";
import SessionsView from "../src/views/SessionsView.vue";
import { useWorkbench } from "../src/stores/workbench";
import { session } from "./fixtures";
const item = { ...session, metadata_hash: "a".repeat(64) };
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  const store = useWorkbench();
  store.workspaces = [
    {
      workspace_id: "workspace-1",
      project_id: "one",
      name: "one",
      path: "/one",
    },
    {
      workspace_id: "workspace-2",
      project_id: "two",
      name: "two",
      path: "/two",
    },
  ];
  store.selectedWorkspace = "workspace-1";
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/sessions", component: SessionsView },
      { path: "/history", component: { template: "<p>history</p>" } },
    ],
  });
  await router.push("/sessions?session=session-1");
  await router.isReady();
  const wrapper = mount(SessionsView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}
it("retains original body and request identity after a lost response", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("lost"))
    .mockResolvedValue(Response.json({}));
  vi.stubGlobal("fetch", fetch);
  const intent: SessionIntent = {
    session: item.session_id,
    action: "update",
    body: { title: "new", expected_metadata_hash: item.metadata_hash },
  };
  await expect(sessionsApi.mutate("one", intent)).rejects.toThrow();
  expect(sessionMutationState("one").pending).toEqual(intent);
  await expect(
    sessionsApi.mutate("one", {
      ...intent,
      body: { ...intent.body, title: "different" },
    }),
  ).rejects.toThrow("尚未确认");
  await sessionsApi.mutate("one", sessionMutationState("one").pending!);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0]![1].body).toEqual(fetch.mock.calls[1]![1].body);
  expect(
    new Headers(fetch.mock.calls[0]![1].headers).get("Idempotency-Key"),
  ).toBe(new Headers(fetch.mock.calls[1]![1].headers).get("Idempotency-Key"));
  expect(sessionMutationState("one").pending).toBeNull();
});
it("keeps a stale editor version until explicit refresh and hides managed sessions", async () => {
  vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
  const list = vi
    .spyOn(sessionsApi, "list")
    .mockResolvedValue([
      item,
      { ...item, session_id: "control", title: "internal", managed: true },
    ]);
  const mutate = vi
    .spyOn(sessionsApi, "mutate")
    .mockRejectedValue(new Error("refresh required"));
  const wrapper = await render();
  expect(wrapper.text()).not.toContain("internal");
  await wrapper.get('input:not([type="checkbox"])').setValue("draft");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(mutate.mock.calls[0]![1].body.expected_metadata_hash).toBe(
    item.metadata_hash,
  );
  expect(wrapper.get("input").element.value).toBe("draft");
  list.mockResolvedValue([
    { ...item, title: "external", metadata_hash: "b".repeat(64) },
  ]);
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "刷新会话版本")!
    .trigger("click");
  await flushPromises();
  expect(wrapper.get("input").element.value).toBe("external");
  expect(wrapper.findAll("button").some((b) => b.text() === "删除空会话")).toBe(
    false,
  );
  wrapper.unmount();
});
it("ignores a previous project's late list", async () => {
  let finish!: (value: (typeof item)[]) => void;
  vi.spyOn(sessionsApi, "list").mockImplementation((owner) =>
    owner === "one"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : Promise.resolve([{ ...item, title: "second" }]),
  );
  const wrapper = await render();
  useWorkbench().selectedWorkspace = "workspace-2";
  await flushPromises();
  finish([item]);
  await flushPromises();
  expect(wrapper.text()).toContain("second");
  expect(wrapper.text()).not.toContain("测试对话");
  wrapper.unmount();
});
it("does not refresh another project's conversation after a late mutation list", async () => {
  let finish!: (value: (typeof item)[]) => void;
  vi.spyOn(sessionsApi, "list")
    .mockResolvedValueOnce([item])
    .mockImplementation((owner) =>
      owner === "one"
        ? new Promise((resolve) => {
            finish = resolve;
          })
        : Promise.resolve([]),
    );
  vi.spyOn(sessionsApi, "mutate").mockResolvedValue({});
  const reads = vi.spyOn(api, "sessions").mockResolvedValue([]);
  const wrapper = await render();
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  useWorkbench().selectedWorkspace = "workspace-2";
  await flushPromises();
  finish([item]);
  await flushPromises();
  expect(reads).not.toHaveBeenCalled();
  wrapper.unmount();
});
it("clears pending intent only on a definite rejected receipt", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "stale" }), {
        status: 409,
        headers: { "X-Eventide-Mutation-Outcome": "rejected" },
      }),
    ),
  );
  const intent: SessionIntent = {
    session: item.session_id,
    action: "delete",
    body: { expected_metadata_hash: item.metadata_hash },
  };
  await expect(sessionsApi.mutate("one", intent)).rejects.toBeInstanceOf(
    ApiError,
  );
  expect(sessionMutationState("one").pending).toBeNull();
});

it("does not discard a renamed draft when reselecting the same session and declining", async () => {
  vi.spyOn(sessionsApi, "list").mockResolvedValue([item]);
  const confirm = vi.fn().mockReturnValue(false);
  vi.stubGlobal("confirm", confirm);
  const wrapper = await render();
  await wrapper.get('input:not([type="checkbox"])').setValue("keep this draft");
  await wrapper.get(".session-grid button").trigger("click");
  expect(
    wrapper.get<HTMLInputElement>('input:not([type="checkbox"])').element.value,
  ).toBe("keep this draft");
  expect(confirm).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});
