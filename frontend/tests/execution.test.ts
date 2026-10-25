import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createRouter, createMemoryHistory } from "vue-router";
import ChatView from "../src/views/ChatView.vue";
import { useWorkbench } from "../src/stores/workbench";
import { provider, session } from "./fixtures";

beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
});
async function setup(bound = true) {
  const store = useWorkbench();
  store.provider = provider;
  store.selectedWorkspace = session.workspace_id;
  store.selectedSession = session.session_id;
  store.sessions[session.workspace_id] = [
    {
      ...session,
      execution: { kind: bound ? "worktree" : "source", name: "session-copy" },
    },
  ];
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: ChatView },
      {
        path: "/evaluations",
        component: { template: "<div>evaluations</div>" },
      },
    ],
  });
  await router.push("/");
  await router.isReady();
  const wrapper = mount(ChatView, {
    global: { plugins: [router], stubs: { GenerationEditor: true } },
  });
  return { store, wrapper };
}
describe("session execution copy controls", () => {
  it("restores a bound copy and cannot send its history back to source", async () => {
    const { store, wrapper } = await setup();
    const submit = vi.spyOn(store, "submit").mockResolvedValue(undefined);
    expect(
      wrapper.get('[aria-label="执行位置"]').attributes("disabled"),
    ).toBeDefined();
    expect(
      (wrapper.get('[aria-label="执行位置"]').element as HTMLSelectElement)
        .value,
    ).toBe("worktree");
    await wrapper.get('[aria-label="运行模式"]').setValue("plan");
    await wrapper
      .get('textarea[aria-label="任务输入"]')
      .setValue("继续检查自己的副本");
    await wrapper.get("form.composer").trigger("submit");
    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "plan", execution_target: "worktree" }),
    );
    wrapper.unmount();
  });
  it("allows an existing copy while source is busy, but resets a new conversation", async () => {
    const { store, wrapper } = await setup();
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "busy-source",
      status: "running",
      execution: { kind: "source" },
    });
    await wrapper
      .get('textarea[aria-label="任务输入"]')
      .setValue("已有副本继续工作");
    expect(
      wrapper.get('button[type="submit"]').attributes("disabled"),
    ).toBeUndefined();
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "new",
      execution: { kind: "source" },
    });
    await wrapper.get('[aria-label="运行模式"]').setValue("agent");
    store.selectedSession = "new";
    await flushPromises();
    expect(
      (wrapper.get('[aria-label="执行位置"]').element as HTMLSelectElement)
        .value,
    ).toBe("source");
    expect(
      (wrapper.get('[aria-label="运行模式"]').element as HTMLSelectElement)
        .value,
    ).toBe("auto");
    await wrapper
      .get('textarea[aria-label="任务输入"]')
      .setValue("新副本需要短期源锁");
    expect(
      wrapper.get('button[type="submit"]').attributes("disabled"),
    ).toBeDefined();
    wrapper.unmount();
  });
  it("preserves the chosen target when polling replaces unchanged session data", async () => {
    const { store, wrapper } = await setup(false);
    await wrapper.get('[aria-label="执行位置"]').setValue("worktree");
    store.sessions[session.workspace_id]![0] = {
      ...store.sessions[session.workspace_id]![0]!,
    };
    await flushPromises();
    expect(
      (wrapper.get('[aria-label="执行位置"]').element as HTMLSelectElement)
        .value,
    ).toBe("worktree");
    const submit = vi.spyOn(store, "submit").mockResolvedValue(undefined);
    await wrapper
      .get('textarea[aria-label="任务输入"]')
      .setValue("必须写入我的独立副本");
    await wrapper.get("form.composer").trigger("submit");
    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({ execution_target: "worktree" }),
    );
    wrapper.unmount();
  });
  it("never submits a snapshot after switching out of read-only mode", async () => {
    const { wrapper } = await setup(false);
    await wrapper.get('[aria-label="运行模式"]').setValue("plan");
    await wrapper.get('[aria-label="执行位置"]').setValue("snapshot");
    await wrapper.get('[aria-label="运行模式"]').setValue("auto");
    expect(
      (wrapper.get('[aria-label="执行位置"]').element as HTMLSelectElement)
        .value,
    ).toBe("source");
    expect(wrapper.find('option[value="snapshot"]').exists()).toBe(false);
    wrapper.unmount();
  });
  it.each(["running", "interrupted"])(
    "routes a visible %s evaluation record to its page without Agent controls",
    async (status) => {
      const { store, wrapper } = await setup(false);
      store.sessions[session.workspace_id]![0] = {
        ...session,
        status,
        execution: { kind: "evaluation", evaluation_id: "evaluation-run" },
        latest_run: { run_id: "evaluation-run", status },
      };
      await flushPromises();
      expect(wrapper.find("form.composer").exists()).toBe(false);
      expect(wrapper.text()).not.toContain("检查并继续");
      expect(wrapper.get('a[href*="evaluation="]').attributes("href")).toBe(
        "/evaluations?evaluation=evaluation-run",
      );
      expect(store.workspaceBusy).toBe(false);
      wrapper.unmount();
    },
  );
});

describe("task knowledge preparation", () => {
  it.each([false, true])(
    "opts into preparation for source/bound-worktree=%s",
    async (bound) => {
      const { wrapper, store } = await setup(bound);
      const submit = vi.spyOn(store, "submit").mockResolvedValue(undefined);
      const option = wrapper.get('[aria-label="任务开始前准备知识索引"]');
      expect((option.element as HTMLInputElement).checked).toBe(false);
      await option.setValue(true);
      await wrapper.get(".index-preparation-options input").setValue(true);
      await wrapper.get('[aria-label="索引范围"]').setValue("all");
      await wrapper
        .get('textarea[aria-label="任务输入"]')
        .setValue("调查取消流程");
      await wrapper.get("form.composer").trigger("submit");
      expect(submit).toHaveBeenCalledWith(
        expect.objectContaining({
          knowledge_index: "prepare",
          code_structure: true,
          source_scope: "all",
          execution_target: bound ? "worktree" : "source",
        }),
      );
      wrapper.unmount();
    },
  );
  it("disables and clears preparation when the target becomes a snapshot", async () => {
    const { wrapper, store } = await setup(false);
    const submit = vi.spyOn(store, "submit").mockResolvedValue(undefined);
    const option = wrapper.get('[aria-label="任务开始前准备知识索引"]');
    await option.setValue(true);
    await wrapper.get('[aria-label="运行模式"]').setValue("plan");
    await wrapper.get('[aria-label="执行位置"]').setValue("snapshot");
    expect(option.attributes("disabled")).toBeDefined();
    expect((option.element as HTMLInputElement).checked).toBe(false);
    await wrapper.get('textarea[aria-label="任务输入"]').setValue("调查快照");
    await wrapper.get("form.composer").trigger("submit");
    expect(submit.mock.calls[0]![0]).not.toHaveProperty("knowledge_index");
    wrapper.unmount();
  });
  it("shows historical missing evidence and follows actual preparation states", async () => {
    const { wrapper, store } = await setup(false);
    expect(wrapper.get('[aria-label="知识检索状态"]').text()).toBe(
      "知识检索：未记录",
    );
    store.sessions[session.workspace_id]![0]!.latest_run = {
      run_id: "retrieval-run",
      status: "running",
      retrieval: { status: "preparing", workspace_id: "actual-copy" },
    };
    await flushPromises();
    expect(wrapper.get('[aria-label="知识检索状态"]').text()).toContain(
      "准备中",
    );
    expect(wrapper.get('[aria-label="知识检索状态"]').text()).toContain(
      "actual-copy",
    );
    store.sessions[session.workspace_id]![0]!.latest_run = {
      run_id: "retrieval-run",
      status: "failed",
      retrieval: {
        status: "failed",
        workspace_id: "actual-copy",
        reason: "budget_exceeded",
      },
    };
    await flushPromises();
    expect(wrapper.get('[aria-label="知识检索状态"]').text()).toContain(
      "budget_exceeded",
    );
    wrapper.unmount();
  });
});
