import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import ChatView from "../src/views/ChatView.vue";
import ThemeControl from "../src/components/ThemeControl.vue";
import ComposerMenu from "../src/components/ComposerMenu.vue";
import ToolActivity from "../src/components/ToolActivity.vue";
import { useWorkbench } from "../src/stores/workbench";
import { provider, session } from "./fixtures";
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  setActivePinia(createPinia());
});
async function setup() {
  const store = useWorkbench();
  store.provider = provider;
  store.selectedWorkspace = session.workspace_id;
  store.selectedSession = session.session_id;
  store.sessions[session.workspace_id] = [
    { ...session, execution: { kind: "source" } },
  ];
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/", component: ChatView }],
  });
  await router.push("/");
  const wrapper = mount(ChatView, {
    global: { plugins: [router], stubs: { GenerationEditor: true } },
  });
  const submit = vi.spyOn(store, "submit").mockResolvedValue(undefined);
  await wrapper.get("textarea").setValue("检查这个项目");
  return { store, wrapper, submit };
}
describe("conversation interaction guardrails", () => {
  it("keeps Enter for newlines and sends on Ctrl or Meta Enter only", async () => {
    const { wrapper, submit } = await setup();
    const input = wrapper.get("textarea");
    await input.trigger("keydown", { key: "Enter" });
    expect(submit).not.toHaveBeenCalled();
    await input.trigger("keydown", { key: "Enter", ctrlKey: true });
    expect(submit).toHaveBeenCalledTimes(1);
    await input.trigger("keydown", { key: "Enter", metaKey: true });
    expect(submit).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });
  it("never submits while an IME composition is active", async () => {
    const { wrapper, submit } = await setup();
    const input = wrapper.get("textarea");
    await input.trigger("compositionstart");
    await input.trigger("keydown", { key: "Enter", ctrlKey: true });
    expect(submit).not.toHaveBeenCalled();
    await input.trigger("compositionend");
    await input.trigger("keydown", {
      key: "Enter",
      ctrlKey: true,
      isComposing: true,
    });
    expect(submit).not.toHaveBeenCalled();
    await input.trigger("keydown", {
      key: "Enter",
      ctrlKey: true,
      keyCode: 229,
    });
    expect(submit).not.toHaveBeenCalled();
    wrapper.unmount();
  });
  it.each(["running", "queued", "waiting_for_user", "interrupted"])(
    "preserves editable draft without sending during %s",
    async (status) => {
      const { wrapper, store, submit } = await setup();
      store.sessions[session.workspace_id]![0]!.latest_run = {
        run_id: "busy",
        status,
      };
      await flushPromises();
      await wrapper.get("textarea").setValue("下一步草稿");
      await wrapper
        .get("textarea")
        .trigger("keydown", { key: "Enter", metaKey: true });
      await wrapper.get("form.composer").trigger("submit");
      expect(submit).not.toHaveBeenCalled();
      expect(store.drafts[session.session_id]).toBe("下一步草稿");
      wrapper.unmount();
    },
  );
  it("blocks keyboard submit when the source workspace is busy", async () => {
    const { wrapper, store, submit } = await setup();
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "other",
      status: "running",
      execution: { kind: "source" },
    });
    await flushPromises();
    await wrapper
      .get("textarea")
      .trigger("keydown", { key: "Enter", ctrlKey: true });
    expect(submit).not.toHaveBeenCalled();
    wrapper.unmount();
  });
  it("does not autoscroll while reading older content and exposes a jump action", async () => {
    const { wrapper, store } = await setup();
    const el = wrapper.get(".conversation").element as HTMLElement;
    Object.defineProperties(el, {
      scrollHeight: { configurable: true, value: 2000 },
      clientHeight: { configurable: true, value: 500 },
    });
    el.scrollTop = 100;
    await wrapper.get(".conversation").trigger("scroll");
    store.messages[session.session_id] = [
      { role: "assistant", content: "New response" },
    ];
    await flushPromises();
    expect(el.scrollTop).toBe(100);
    expect(wrapper.find(".jump-latest").exists()).toBe(true);
    await wrapper.get(".jump-latest").trigger("click");
    await flushPromises();
    expect(el.scrollTop).toBe(2000);
    expect(wrapper.find(".jump-latest").exists()).toBe(false);
    wrapper.unmount();
  });
  it("offers Continue only for a safe checkpoint, never for unknown outcomes", async () => {
    const { wrapper, store } = await setup();
    store.sessions[session.workspace_id]![0]!.latest_run = {
      run_id: "parked",
      status: "interrupted",
      reason: "unknown_tool_outcome",
    };
    await flushPromises();
    expect(wrapper.get(".parked-card").text()).toContain("不允许直接继续");
    expect(wrapper.get(".parked-card").find("button").exists()).toBe(false);
    wrapper.unmount();
  });
});
describe("theme and disclosure controls", () => {
  it("persists a theme override and restores system mode", async () => {
    const wrapper = mount(ThemeControl);
    await wrapper.get("select").setValue("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("eventide.theme")).toBe("dark");
    await wrapper.get("select").setValue("system");
    expect(document.documentElement.dataset.theme).toBe("system");
    wrapper.unmount();
  });
  it("opens a menu, closes on Escape and outside pointer interaction", async () => {
    const wrapper = mount(ComposerMenu, {
      props: { label: "Skills" },
      slots: { default: "Actual configured skills" },
      attachTo: document.body,
    });
    await wrapper.get("button").trigger("click");
    expect(wrapper.find(".menu-popover").exists()).toBe(true);
    await wrapper.trigger("keydown", { key: "Escape" });
    expect(wrapper.find(".menu-popover").exists()).toBe(false);
    await wrapper.get("button").trigger("click");
    document.body.dispatchEvent(
      new PointerEvent("pointerdown", { bubbles: true }),
    );
    await flushPromises();
    expect(wrapper.find(".menu-popover").exists()).toBe(false);
    wrapper.unmount();
  });
  it("keeps tool details inert and reports actual failure status", () => {
    const wrapper = mount(ToolActivity, {
      props: {
        tools: {
          call: {
            name: "read_file",
            status: "completed",
            is_error: true,
            output: "<script>alert(1)</script>",
          },
        },
        count: 1,
      },
    });
    expect(wrapper.text()).toContain("执行失败");
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.text()).toContain("<script>");
    wrapper.unmount();
  });
});
