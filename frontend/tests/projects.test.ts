import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ProjectsView from "../src/views/ProjectsView.vue";
import { api } from "../src/api/client";
const startup = {
  workspace_id: "startup",
  name: "startup",
  path: "/startup",
  startup_workspace: true,
};
const project = { workspace_id: "other", name: "other", path: "/other" };
beforeEach(() => {
  setActivePinia(createPinia());
  sessionStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("keeps an uncertain removal retryable after the record disappears and page reloads", async () => {
  const list = vi
    .spyOn(api, "workspaces")
    .mockResolvedValue([startup, project]);
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new Error("response lost"))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ removed: "other" }), { status: 200 }),
    );
  vi.stubGlobal("fetch", fetch);
  let wrapper = mount(ProjectsView);
  await flushPromises();
  await wrapper.get("article button").trigger("click");
  await wrapper.get('input[type="checkbox"]').setValue(true);
  await wrapper.get('[aria-label="移除项目确认"] button').trigger("click");
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  await flushPromises();
  expect(sessionStorage.getItem("eventide.pending-workspace-removal")).toBe(
    "other",
  );
  wrapper.unmount();
  list.mockResolvedValue([startup]);
  wrapper = mount(ProjectsView);
  await flushPromises();
  expect(wrapper.get('[aria-label="移除项目确认"]').text()).toContain("other");
  await wrapper.get('input[type="checkbox"]').setValue(true);
  await wrapper.get('[aria-label="移除项目确认"] button').trigger("click");
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  await flushPromises();
  const keys = fetch.mock.calls.map(
    (call) => call[1].headers["Idempotency-Key"],
  );
  expect(keys[0]).toBe(keys[1]);
  expect(
    sessionStorage.getItem("eventide.pending-workspace-removal"),
  ).toBeNull();
  expect(wrapper.get('[role="status"]').text()).toContain("磁盘文件保留");
  expect(wrapper.find("article button").exists()).toBe(false);
  wrapper.unmount();
});
