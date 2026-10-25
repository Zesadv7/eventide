import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import ChatView from "../src/views/ChatView.vue";
import SandboxEvidence from "../src/components/SandboxEvidence.vue";
import { useWorkbench } from "../src/stores/workbench";
import type { Execution, Session } from "../src/api/types";
import { provider, session } from "./fixtures";
const execution: Execution = {
  kind: "sandbox",
  sandbox_id: "sb-1",
  working_directory: "/candidate",
  skipped: 3,
  profile: {
    kind: "sandbox",
    image: "sha256:immutable",
    requested_image: "dev:local",
    network: "none",
    memory_mb: 512,
    cpus: 1,
    pids_limit: 128,
    timeout: 120,
    output_limit: 25000,
  },
};
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
});
async function setup(value: Partial<Session> = {}, pending = false) {
  const store = useWorkbench();
  store.provider = provider;
  store.selectedWorkspace = session.workspace_id;
  store.selectedSession = session.session_id;
  store.sessions[session.workspace_id] = [
    { ...session, latest_run: null, execution: { kind: "source" }, ...value },
  ];
  if (pending)
    store.pending[session.session_id] = {
      key: "same-key",
      body: {
        prompt: "pending",
        mode: "auto",
        execution_target: "source",
        execution_profile: "sandbox",
        sandbox_image: "pending:local",
        attachment_ids: [],
      },
    };
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/", component: ChatView }],
  });
  await router.push("/");
  const wrapper = mount(ChatView, {
    global: { plugins: [router], stubs: { GenerationEditor: true } },
  });
  const submit = vi.spyOn(store, "submit").mockResolvedValue(undefined);
  await wrapper.get('textarea[aria-label="任务输入"]').setValue("检查项目");
  return { store, wrapper, submit };
}
describe("optional offline sandbox controls", () => {
  it("keeps local default and requires an image for a new sandbox without changing permission mode", async () => {
    const { wrapper, submit } = await setup();
    expect(
      (wrapper.get('[aria-label="执行环境"]').element as HTMLSelectElement)
        .value,
    ).toBe("local");
    expect(wrapper.find('[aria-label="沙箱镜像"]').exists()).toBe(false);
    await wrapper.get('[aria-label="运行模式"]').setValue("plan");
    await wrapper.get('[aria-label="执行位置"]').setValue("snapshot");
    await wrapper.get('[aria-label="执行环境"]').setValue("sandbox");
    expect(wrapper.find('[aria-label="执行位置"]').exists()).toBe(false);
    expect(wrapper.find('[aria-label="Skills"]').exists()).toBe(false);
    expect(
      wrapper
        .get('[aria-label="任务开始前准备知识索引"]')
        .attributes("disabled"),
    ).toBeDefined();
    expect(
      (
        wrapper.get('[aria-label="任务开始前准备知识索引"]')
          .element as HTMLInputElement
      ).checked,
    ).toBe(false);
    await wrapper.get("form").trigger("submit");
    expect(submit).not.toHaveBeenCalled();
    await wrapper.get('[aria-label="沙箱镜像"]').setValue("  dev:local  ");
    await wrapper.get("form").trigger("submit");
    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: "plan",
        execution_target: "source",
        execution_profile: "sandbox",
        sandbox_image: "dev:local",
      }),
    );
    expect(wrapper.text()).toContain("不会回退到本机执行");
    expect(wrapper.find(".sandbox-evidence").exists()).toBe(false);
    wrapper.unmount();
  });
  it.each(["Docker unavailable", "Sandbox image not present locally"])(
    "shows %s without falling back or discarding the selected image",
    async (message) => {
      const { store, wrapper, submit } = await setup();
      await wrapper.get('[aria-label="执行环境"]').setValue("sandbox");
      await wrapper.get('[aria-label="沙箱镜像"]').setValue("dev:local");
      submit.mockRejectedValue(new Error(message));
      await wrapper.get("form").trigger("submit");
      await flushPromises();
      expect(store.error).toBe(message);
      expect(submit).toHaveBeenCalledTimes(1);
      expect(
        (wrapper.get('[aria-label="执行环境"]').element as HTMLSelectElement)
          .value,
      ).toBe("sandbox");
      expect(
        (wrapper.get('[aria-label="沙箱镜像"]').element as HTMLInputElement)
          .value,
      ).toBe("dev:local");
      expect(wrapper.find(".sandbox-evidence").exists()).toBe(false);
      wrapper.unmount();
    },
  );
  it("preserves a chosen sandbox and image across polling of the same empty Session", async () => {
    const { store, wrapper } = await setup();
    await wrapper.get('[aria-label="执行环境"]').setValue("sandbox");
    await wrapper.get('[aria-label="沙箱镜像"]').setValue("dev:local");
    store.sessions[session.workspace_id]![0] = {
      ...store.sessions[session.workspace_id]![0]!,
    };
    await flushPromises();
    expect(
      (wrapper.get('[aria-label="沙箱镜像"]').element as HTMLInputElement)
        .value,
    ).toBe("dev:local");
    wrapper.unmount();
  });
  it("does not add sandbox overrides to ordinary local submissions", async () => {
    const { wrapper, submit } = await setup();
    await wrapper.get("form").trigger("submit");
    expect(submit.mock.calls[0]?.[0]).not.toHaveProperty("execution_profile");
    expect(submit.mock.calls[0]?.[0]).not.toHaveProperty("sandbox_image");
    wrapper.unmount();
  });
  it("freezes an existing local session even when its historical run has no profile field", async () => {
    const { wrapper } = await setup({ latest_run: session.latest_run });
    expect(
      wrapper.get('[aria-label="执行环境"]').attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.text()).toContain("请新建对话");
    wrapper.unmount();
  });
  it("reuses a bound image and candidate for new messages while source is busy", async () => {
    const { store, wrapper, submit } = await setup({
      execution_profile: "sandbox",
      sandbox_profile: execution.profile,
      execution,
      latest_run: { ...session.latest_run!, execution },
    });
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "other",
      status: "running",
      execution: { kind: "source" },
    });
    await flushPromises();
    expect(
      wrapper.get('[aria-label="执行环境"]').attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.find('[aria-label="沙箱镜像"]').exists()).toBe(false);
    await wrapper.get("form").trigger("submit");
    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({
        execution_profile: "sandbox",
        sandbox_image: null,
        execution_target: "source",
      }),
    );
    expect(wrapper.get(".sandbox-evidence").text()).toContain(
      "sha256:immutable",
    );
    wrapper.unmount();
  });
  it("keeps Continue free of execution overrides", async () => {
    const { store, wrapper, submit } = await setup({
      execution_profile: "sandbox",
      execution,
      latest_run: { ...session.latest_run!, status: "interrupted", execution },
    });
    const resume = vi.spyOn(store, "continueRun").mockResolvedValue({
      run_id: "continued",
      session_id: session.session_id,
      status: "accepted",
    });
    await wrapper.get(".parked-card button").trigger("click");
    expect(resume).toHaveBeenCalledWith();
    expect(submit).not.toHaveBeenCalled();
    wrapper.unmount();
  });
  it("restores an unknown sandbox request without advertising readiness or permitting a profile change", async () => {
    const { wrapper } = await setup({}, true);
    expect(
      (wrapper.get('[aria-label="执行环境"]').element as HTMLSelectElement)
        .value,
    ).toBe("sandbox");
    expect(
      wrapper.get('[aria-label="执行环境"]').attributes("disabled"),
    ).toBeDefined();
    expect(
      (wrapper.get('[aria-label="沙箱镜像"]').element as HTMLInputElement)
        .value,
    ).toBe("pending:local");
    expect(
      wrapper.get('[aria-label="沙箱镜像"]').attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.text()).toContain("选择沙箱不代表 Docker 或镜像已就绪");
    expect(wrapper.text()).toContain("仅使用受信项目和镜像");
    wrapper.unmount();
  });
  it("resets draft profile selection on session switch and ignores a late previous-session error", async () => {
    const { store, wrapper, submit } = await setup();
    let reject!: (error: Error) => void;
    submit.mockImplementation(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    );
    await wrapper.get('[aria-label="执行环境"]').setValue("sandbox");
    await wrapper.get('[aria-label="沙箱镜像"]').setValue("dev:local");
    await wrapper.get("form").trigger("submit");
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "new",
      latest_run: null,
    });
    store.selectedSession = "new";
    await flushPromises();
    reject(new Error("old Docker error"));
    await flushPromises();
    expect(store.error).toBe("");
    expect(
      (wrapper.get('[aria-label="执行环境"]').element as HTMLSelectElement)
        .value,
    ).toBe("local");
    expect(wrapper.find(".sandbox-evidence").exists()).toBe(false);
    wrapper.unmount();
  });
  it("does not treat another sandbox as a source lock", async () => {
    const { store, wrapper } = await setup();
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "sandbox",
      status: "running",
      execution,
    });
    expect(store.workspaceBusy).toBe(false);
    wrapper.unmount();
  });
  it("exports only the encoded run's patch and displays actual constraints rather than selected input", () => {
    const wrapper = mount(SandboxEvidence, {
      props: { execution, runId: "run/a" },
    });
    expect(wrapper.get("a").attributes("href")).toBe(
      "/api/runs/run%2Fa/sandbox-diff",
    );
    expect(wrapper.get("a").attributes("download")).toBeDefined();
    expect(wrapper.text()).toContain("sha256:immutable");
    expect(wrapper.text()).toContain("512 MiB");
    expect(wrapper.text()).toContain("进程 128");
    expect(wrapper.text()).toContain("25000 字节");
    expect(wrapper.text()).toContain("当前状态");
    expect(wrapper.text()).toContain("不会自动修改源码");
    wrapper.unmount();
  });
});
