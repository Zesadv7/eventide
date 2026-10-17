import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { defineComponent } from "vue";
import App from "../src/App.vue";
import { useWorkbench } from "../src/stores/workbench";
import { useUnsavedChanges } from "../src/composables/useUnsavedChanges";
import { session } from "./fixtures";
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  setActivePinia(createPinia());
});
async function setup(path = "/chat/session-1", dirty = false) {
  const store = useWorkbench();
  store.loading = false;
  store.selectedWorkspace = "workspace-1";
  store.selectedSession = "session-1";
  store.workspaces = [
    { workspace_id: "workspace-1", name: "First", path: "/first" },
    { workspace_id: "workspace-2", name: "Second", path: "/second" },
  ];
  store.sessions["workspace-1"] = [
    { ...session },
    { ...session, session_id: "session-other", title: "Other" },
  ];
  store.sessions["workspace-2"] = [
    { ...session, workspace_id: "workspace-2", session_id: "session-2" },
  ];
  vi.spyOn(store, "initialize").mockResolvedValue();
  vi.spyOn(store, "startPolling").mockImplementation(() => {});
  const Guard = defineComponent({
    setup() {
      useUnsavedChanges(() => dirty);
      return {};
    },
    template: "<div>Settings draft</div>",
  });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/chat/:sessionId?", component: { template: "<div>Chat</div>" } },
      { path: "/settings", component: Guard },
      { path: "/:pathMatch(.*)*", component: { template: "<div>Page</div>" } },
    ],
  });
  await router.push(path);
  const wrapper = mount(App, {
    global: {
      plugins: [router],
      stubs: { PageBack: true, ProjectFolderPicker: true },
    },
  });
  await flushPromises();
  return { store, router, wrapper };
}
describe("shell navigation and project identity", () => {
  it("synchronizes the chat URL after switching projects", async () => {
    const { store, router, wrapper } = await setup();
    const select = vi
      .spyOn(store, "selectWorkspace")
      .mockImplementation(async (id) => {
        store.selectedWorkspace = id;
        store.selectedSession = "session-2";
      });
    await wrapper.get("#workspace").setValue("workspace-2");
    await flushPromises();
    expect(select).toHaveBeenCalledWith("workspace-2");
    expect(router.currentRoute.value.path).toBe("/chat/session-2");
    wrapper.unmount();
  });
  it("does not mutate project selection after rejecting a dirty-form guard", async () => {
    const { store, router, wrapper } = await setup("/settings", true);
    vi.stubGlobal(
      "confirm",
      vi.fn(() => false),
    );
    const select = vi.spyOn(store, "selectWorkspace").mockResolvedValue();
    await wrapper.get("#workspace").setValue("workspace-2");
    await flushPromises();
    expect(select).not.toHaveBeenCalled();
    expect((wrapper.get("#workspace").element as HTMLSelectElement).value).toBe(
      "workspace-1",
    );
    expect(router.currentRoute.value.path).toBe("/settings");
    wrapper.unmount();
  });
  it("checks navigation before creating a new conversation", async () => {
    const { store, wrapper } = await setup("/settings", true);
    vi.stubGlobal(
      "confirm",
      vi.fn(() => false),
    );
    const create = vi.spyOn(store, "createSession").mockResolvedValue("new");
    await wrapper.get(".new-session").trigger("click");
    await flushPromises();
    expect(create).not.toHaveBeenCalled();
    wrapper.unmount();
  });
  it("preserves the exact native folder path including trailing spaces", async () => {
    const { store, wrapper } = await setup();
    const add = vi.spyOn(store, "addWorkspace").mockResolvedValue();
    await wrapper.get('[aria-label="添加项目"]').trigger("click");
    wrapper
      .findComponent({ name: "ProjectFolderPicker" })
      .vm.$emit("update:modelValue", "/project with spaces ");
    await flushPromises();
    await wrapper.get("form.add-workspace").trigger("submit");
    await flushPromises();
    expect(add).toHaveBeenCalledWith("/project with spaces ");
    wrapper.unmount();
  });
  it("persists collapsed navigation and removes it from keyboard traversal", async () => {
    const { wrapper } = await setup();
    await wrapper.get('[aria-label="收起侧栏"]').trigger("click");
    expect(localStorage.getItem("eventide.sidebar.collapsed")).toBe("true");
    expect(wrapper.get("aside.sidebar").attributes("inert")).toBeDefined();
    wrapper.unmount();
  });
});
