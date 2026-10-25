import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import App from "../src/App.vue";
import ContractBarrier from "../src/components/ContractBarrier.vue";
import { checkRuntimeContract } from "../src/api/contract";
import { useContractGate } from "../src/stores/contract";
import { useWorkbench } from "../src/stores/workbench";

const compatibleBody = {
  contract_version: 1,
  schema_version: 6,
  runtime_version: "0.5.0",
  capabilities: ["projects", "sessions"],
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("runtime contract classification", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("accepts an exact contract version match", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(compatibleBody)),
    );
    const check = await checkRuntimeContract();
    expect(check).toEqual({ kind: "compatible", contract: compatibleBody });
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe("/api/runtime/contract");
  });

  it("reports a newer server contract as an upgrade of the frontend", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ ...compatibleBody, contract_version: 2 }),
        ),
    );
    const check = await checkRuntimeContract();
    expect(check.kind).toBe("incompatible");
    if (check.kind === "incompatible") {
      expect(check.serverVersion).toBe(2);
      expect(check.message).toContain("v2");
      expect(check.message).toContain("升级前端");
    }
  });

  it("reports an older server contract as an upgrade of the service", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ ...compatibleBody, contract_version: 0 }),
        ),
    );
    const check = await checkRuntimeContract();
    expect(check.kind).toBe("incompatible");
    if (check.kind === "incompatible")
      expect(check.message).toContain("升级服务端");
  });

  it("treats a missing contract endpoint as an old backend, not a network fault", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ code: "not_found" }, 404)),
    );
    const check = await checkRuntimeContract();
    expect(check.kind).toBe("incompatible");
    if (check.kind === "incompatible") {
      expect(check.serverVersion).toBeNull();
      expect(check.message).toContain("契约版本接口");
    }
  });

  it("treats an unparseable or versionless payload as an unknown version", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ hello: "world" })),
    );
    expect((await checkRuntimeContract()).kind).toBe("incompatible");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("not json", { status: 200 })),
    );
    expect((await checkRuntimeContract()).kind).toBe("incompatible");
  });

  it("never mistakes transport failures or server faults for version problems", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    const offline = await checkRuntimeContract();
    expect(offline.kind).toBe("unavailable");
    if (offline.kind === "unavailable")
      expect(offline.message).toContain("不是版本不匹配");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("boom", { status: 500 })),
    );
    const broken = await checkRuntimeContract();
    expect(broken.kind).toBe("unavailable");
    if (broken.kind === "unavailable") expect(broken.message).toContain("500");
  });
});

describe("startup compatibility gate", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    setActivePinia(createPinia());
  });
  afterEach(() => vi.unstubAllGlobals());

  async function mountApp(responses: unknown[]) {
    const store = useWorkbench();
    vi.spyOn(store, "initialize").mockResolvedValue();
    vi.spyOn(store, "startPolling").mockImplementation(() => {});
    const fetchMock = vi.fn();
    responses.forEach((body) =>
      fetchMock.mockImplementationOnce(() => Promise.resolve(body)),
    );
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse(compatibleBody)),
    );
    vi.stubGlobal("fetch", fetchMock);
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        {
          path: "/chat/:sessionId?",
          component: { template: "<div>Chat</div>" },
        },
        {
          path: "/:pathMatch(.*)*",
          component: { template: "<div>Page</div>" },
        },
      ],
    });
    await router.push("/chat/session-1");
    const wrapper = mount(App, {
      global: {
        plugins: [router],
        stubs: {
          PageBack: true,
          ProjectFolderPicker: true,
          ThemeControl: true,
        },
      },
    });
    await flushPromises();
    return { store, wrapper, fetchMock };
  }

  it("boots the workbench only after a compatible contract", async () => {
    const { store, wrapper } = await mountApp([jsonResponse(compatibleBody)]);
    expect(wrapper.find(".app-shell").exists()).toBe(true);
    expect(store.initialize).toHaveBeenCalled();
    expect(wrapper.find(".contract-barrier").exists()).toBe(false);
    wrapper.unmount();
  });

  it("blocks every write surface behind a visible version error with retry", async () => {
    const { wrapper } = await mountApp([
      jsonResponse({ ...compatibleBody, contract_version: 2 }),
    ]);
    expect(wrapper.find(".contract-barrier").exists()).toBe(true);
    expect(wrapper.find(".app-shell").exists()).toBe(false);
    expect(wrapper.find("#workspace").exists()).toBe(false);
    expect(wrapper.find(".new-session").exists()).toBe(false);
    expect(wrapper.text()).toContain("前后端版本不兼容");
    expect(wrapper.text()).toContain("v2");
    const retry = wrapper.get(".contract-retry");
    await retry.trigger("click");
    await flushPromises();
    expect(wrapper.find(".app-shell").exists()).toBe(true);
    wrapper.unmount();
  });

  it("keeps availability failures retryable without upgrade wording", async () => {
    const { wrapper } = await mountApp([new Response("boom", { status: 503 })]);
    expect(wrapper.find(".contract-barrier").exists()).toBe(true);
    expect(wrapper.find(".app-shell").exists()).toBe(false);
    expect(wrapper.text()).toContain("不是版本不匹配");
    expect(wrapper.text()).not.toContain("升级");
    await wrapper.get(".contract-retry").trigger("click");
    await flushPromises();
    expect(wrapper.find(".app-shell").exists()).toBe(true);
    wrapper.unmount();
  });

  it("keeps the check inflight-safe when the retry is pressed repeatedly", async () => {
    setActivePinia(createPinia());
    const gate = useContractGate();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation(() =>
          Promise.resolve(jsonResponse(compatibleBody)),
        ),
    );
    await Promise.all([gate.runCheck(), gate.runCheck(), gate.runCheck()]);
    expect(gate.compatible).toBe(true);
    expect(vi.mocked(fetch).mock.calls.length).toBe(1);
  });
});

describe("contract barrier rendering", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("shows server facts and stays inert while checking", async () => {
    const gate = useContractGate();
    const wrapper = mount(ContractBarrier);
    expect(wrapper.text()).toContain("正在检查服务兼容性");
    expect(wrapper.find(".contract-retry").exists()).toBe(false);
    gate.result = {
      kind: "incompatible",
      message: "服务端契约版本为 v3，当前前端支持 v1。",
      serverVersion: 3,
      server: { ...compatibleBody, contract_version: 3 },
    };
    gate.status = "incompatible";
    await flushPromises();
    expect(wrapper.text()).toContain("服务端数据版本");
    expect(wrapper.text()).toContain("v6");
    wrapper.unmount();
  });
});
