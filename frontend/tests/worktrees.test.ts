import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  worktreesApi,
  worktreeRequestState,
  type WorktreeCatalog,
} from "../src/api/worktrees";
import WorktreesView from "../src/views/WorktreesView.vue";
import { useWorkbench } from "../src/stores/workbench";
const catalog: WorktreeCatalog = {
  project_id: "one",
  catalog_hash: "a".repeat(64),
  worktrees: [
    {
      name: "kept",
      workspace_id: "w",
      path: "/copy",
      repository: "/one",
      git_common_dir: "/one/.git",
      branch: "codex/copy",
      base_commit: "a".repeat(40),
      kept: true,
      history_protected: true,
    },
  ],
};
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  const store = useWorkbench();
  store.workspaces = [
    { workspace_id: "w", project_id: "one", name: "one", path: "/one" },
    { workspace_id: "other", project_id: "two", name: "two", path: "/two" },
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
    routes: [{ path: "/worktrees", component: WorktreesView }],
  });
  await router.push("/worktrees");
  await router.isReady();
  const wrapper = mount(WorktreesView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}
it("retains exact request identity and body across uncertain acceptance", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("lost"))
    .mockResolvedValue(Response.json({ run_id: "r" }));
  vi.stubGlobal("fetch", fetch);
  const input = {
    action: "create" as const,
    name: "copy",
    ref: "HEAD",
    expected_catalog_hash: catalog.catalog_hash,
  };
  await expect(worktreesApi.create("one", input)).rejects.toThrow();
  expect(worktreeRequestState("one").pending).toEqual(input);
  await expect(
    worktreesApi.create("one", { ...input, name: "other" }),
  ).rejects.toThrow("尚未确认");
  await worktreesApi.create("one", worktreeRequestState("one").pending!);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0]![1].body).toBe(fetch.mock.calls[1]![1].body);
  expect(
    new Headers(fetch.mock.calls[0]![1].headers).get("Idempotency-Key"),
  ).toBe(new Headers(fetch.mock.calls[1]![1].headers).get("Idempotency-Key"));
});
it("does not offer cleanup for copies with history", async () => {
  vi.spyOn(worktreesApi, "catalog").mockResolvedValue(catalog);
  vi.spyOn(worktreesApi, "list").mockResolvedValue([]);
  const wrapper = await render();
  expect(wrapper.text()).toContain("只能保留");
  expect(wrapper.find('input[type="radio"]').exists()).toBe(false);
  expect(
    wrapper.findAll("button").some((b) => b.text().startsWith("清理副本")),
  ).toBe(false);
  wrapper.unmount();
});
it("ignores a previous project catalog arriving after switching", async () => {
  let finish!: (value: WorktreeCatalog) => void;
  vi.spyOn(worktreesApi, "catalog").mockImplementation((owner) =>
    owner === "one"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : Promise.resolve({ ...catalog, project_id: "two", worktrees: [] }),
  );
  vi.spyOn(worktreesApi, "list").mockResolvedValue([]);
  const wrapper = await render();
  useWorkbench().selectedWorkspace = "other";
  await flushPromises();
  finish(catalog);
  await flushPromises();
  expect(wrapper.findAll(".copy")).toHaveLength(0);
  wrapper.unmount();
});
