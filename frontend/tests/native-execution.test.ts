import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import NativeExecutionOptions from "../src/components/NativeExecutionOptions.vue";
import { request } from "../src/api/client";
vi.mock("../src/api/client", () => ({ request: vi.fn() }));
const capability = {
  platform: "Darwin",
  default_backend: "seatbelt",
  profiles: ["read-only", "workspace-write"],
  verification: "unverified",
  verification_detail: "检测存在不代表内核隔离已验证",
  backends: [
    {
      kind: "seatbelt",
      available: true,
      detail: "已找到",
      binary: "/usr/bin/sandbox-exec",
      support: {},
    },
  ],
};
afterEach(() => {
  vi.resetAllMocks();
});
describe("native environment selection", () => {
  it("reports availability separately from verification and freezes saved controls", async () => {
    vi.mocked(request).mockResolvedValue(capability);
    const wrapper = mount(NativeExecutionOptions, {
      props: { profile: "read-only", backend: "seatbelt", frozen: true },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("检测存在不代表内核隔离已验证");
    expect(wrapper.text()).toContain("Shell、Skills、MCP");
    expect(wrapper.emitted("ready")?.at(-1)).toEqual([true]);
    expect(
      wrapper.get('[aria-label="原生文件权限"]').attributes("disabled"),
    ).toBeDefined();
    expect(
      wrapper.get('[aria-label="原生平台后端"]').attributes("disabled"),
    ).toBeDefined();
    wrapper.unmount();
  });
  it("keeps unavailable environments disabled and shows the actual reason", async () => {
    vi.mocked(request).mockResolvedValue({
      ...capability,
      backends: [
        {
          ...capability.backends[0],
          available: false,
          detail: "binary missing",
        },
      ],
    });
    const wrapper = mount(NativeExecutionOptions, {
      props: { profile: "workspace-write", backend: "", frozen: false },
    });
    await flushPromises();
    expect(wrapper.emitted("ready")?.at(-1)).toEqual([false]);
    expect(wrapper.text()).toContain("binary missing");
    wrapper.unmount();
  });
  it("ignores a delayed capability response after leaving the conversation", async () => {
    let resolve!: (value: unknown) => void;
    vi.mocked(request).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const ready = vi.fn();
    const wrapper = mount(NativeExecutionOptions, {
      props: {
        profile: "read-only",
        backend: "",
        frozen: false,
        onReady: ready,
      },
    });
    wrapper.unmount();
    resolve(capability);
    await flushPromises();
    expect(ready.mock.calls).toEqual([[false]]);
  });
});
