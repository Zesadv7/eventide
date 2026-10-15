import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { api } from "../src/api/client";
import { hasPendingProbe, probeApi, type ProbeResult } from "../src/api/probe";
import type { ProviderInput } from "../src/api/types";
import SettingsView from "../src/views/SettingsView.vue";
import { useWorkbench } from "../src/stores/workbench";
import { ledger, provider } from "./fixtures";

const body: ProviderInput = {
  provider: "openai_responses",
  model: "candidate-model",
  base_url: null,
  clear_api_key: false,
  api_key: "candidate-secret",
};
const result: ProbeResult = {
  success: false,
  provider: "openai_responses",
  model: "candidate-model",
  run_id: "probe-fixture",
  usage: ledger,
  latency_ms: null,
  checked_at: null,
  recovered: true,
  message: "已恢复停止记录",
  error_type: "interrupted",
};
const mountSettings = () =>
  mount(SettingsView, {
    global: { stubs: { GenerationEditor: true } },
  });
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  setActivePinia(createPinia());
  vi.spyOn(api, "provider").mockResolvedValue(provider);
});
afterEach(() => vi.unstubAllGlobals());

describe("connection probe request identity", () => {
  it("keeps unknown identities across refresh and blocks edited candidate requests", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(Response.json(result));
    vi.stubGlobal("fetch", fetch);
    await expect(probeApi.test(body)).rejects.toThrow("offline");
    expect(hasPendingProbe()).toBe(true);
    expect(
      JSON.stringify({ ...sessionStorage, ...localStorage }),
    ).not.toContain("candidate-secret");
    expect(
      JSON.stringify({ ...sessionStorage, ...localStorage }),
    ).not.toContain("candidate-model");
    await expect(probeApi.test({ ...body, model: "changed" })).rejects.toThrow(
      "原候选配置",
    );
    expect(fetch).toHaveBeenCalledTimes(1);
    const reloaded = await import("../src/api/probe");
    expect(await reloaded.probeApi.test(body)).toEqual(result);
    expect(fetch.mock.calls[0]![1].headers["Idempotency-Key"]).toBe(
      fetch.mock.calls[1]![1].headers["Idempotency-Key"],
    );
    expect(sessionStorage.length).toBe(0);
  });

  it("retains an unmarked 409 but clears a definite rejection before a new intent", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({ detail: "still running" }, { status: 409 }),
      )
      .mockResolvedValueOnce(
        Response.json({ detail: "invalid" }, { status: 422 }),
      )
      .mockResolvedValueOnce(Response.json(result));
    vi.stubGlobal("fetch", fetch);
    await expect(probeApi.test(body)).rejects.toThrow("still running");
    expect(hasPendingProbe()).toBe(true);
    await expect(probeApi.test(body)).rejects.toThrow("invalid");
    expect(hasPendingProbe()).toBe(false);
    await probeApi.test(body);
    const keys = fetch.mock.calls.map(
      (call) => call[1].headers["Idempotency-Key"],
    );
    expect(keys[0]).toBe(keys[1]);
    expect(keys[1]).not.toBe(keys[2]);
  });

  it("blocks a second caller before hashing and dispatch", async () => {
    let finish!: (response: Response) => void;
    const fetch = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const pending = probeApi.test(body);
    await expect(probeApi.test(body)).rejects.toThrow("正在进行");
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    finish(Response.json(result));
    await pending;
  });
});

describe("settings connection test", () => {
  it("never automatically probes and keeps stored connection distinct from the candidate", async () => {
    const test = vi.spyOn(probeApi, "test").mockResolvedValue({
      ...result,
      success: true,
      message: "连接成功",
      recovered: false,
      latency_ms: 12,
      usage: {
        ...ledger,
        reported_totals: { input_tokens: 8, output_tokens: 2 },
        unknown_attempts: 0,
        incomplete_attempts: 0,
        complete: true,
      },
    });
    const save = vi.spyOn(api, "saveProvider");
    const wrapper = mountSettings();
    await flushPromises();
    expect(test).not.toHaveBeenCalled();
    await wrapper.get('input[maxlength="256"]').setValue("candidate-model");
    await wrapper.get('input[type="password"]').setValue("candidate-secret");
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "测试连接")!
      .trigger("click");
    await flushPromises();
    expect(test).toHaveBeenCalledWith(
      expect.objectContaining({
        model: "candidate-model",
        api_key: "candidate-secret",
      }),
    );
    expect(save).not.toHaveBeenCalled();
    expect(useWorkbench().provider).toEqual(provider);
    expect(wrapper.text()).toContain("连接测试成功");
    expect(wrapper.text()).toContain("probe-fixture");
    expect(wrapper.text()).toContain("UTC当日预算");
    expect(wrapper.text()).toContain("已报告输入Token8");
    expect(
      wrapper.find('a[href="/api/runs/probe-fixture/usage"]').exists(),
    ).toBe(true);
    expect(
      JSON.stringify({ ...sessionStorage, ...localStorage }),
    ).not.toContain("candidate-secret");
    expect(wrapper.text()).not.toContain("candidate-secret");
    wrapper.unmount();
  });

  it("freezes all candidate fields and configuration actions until the probe finishes", async () => {
    let finish!: (value: ProbeResult) => void;
    const test = vi.spyOn(probeApi, "test").mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const save = vi.spyOn(api, "saveProvider");
    const reset = vi.spyOn(api, "resetProvider");
    const wrapper = mountSettings();
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "测试连接")!
      .trigger("click");
    expect(wrapper.get("fieldset").attributes("disabled")).toBeDefined();
    for (const label of ["保存连接", "恢复启动配置", "测试中…"])
      expect(
        wrapper
          .findAll("button")
          .find((button) => button.text() === label)!
          .attributes("disabled"),
      ).toBeDefined();
    await wrapper.get("form").trigger("submit");
    expect(save).not.toHaveBeenCalled();
    expect(reset).not.toHaveBeenCalled();
    expect(test).toHaveBeenCalledTimes(1);
    finish(result);
    await flushPromises();
    expect(wrapper.get("fieldset").attributes("disabled")).toBeUndefined();
    expect(wrapper.text()).toContain("连接测试未成功");
    expect(wrapper.text()).toContain("已报告输入Token未报告");
    expect(wrapper.text()).toContain("耗时：未知");
    expect(wrapper.text()).toContain("未知用量请求1");
    expect(wrapper.text()).toContain("未结算额度继续保留");
    wrapper.unmount();
  });

  it("restores only pending identity after remount, requiring the original secret again", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network lost candidate-secret"))
      .mockResolvedValueOnce(Response.json(result));
    vi.stubGlobal("fetch", fetch);
    const first = mountSettings();
    await flushPromises();
    await first.get('input[type="password"]').setValue("candidate-secret");
    await first
      .findAll("button")
      .find((button) => button.text() === "测试连接")!
      .trigger("click");
    await vi.waitFor(() => expect(first.text()).toContain("尚未确认"));
    expect(first.text()).not.toContain("candidate-secret");
    first.unmount();
    const second = mountSettings();
    await flushPromises();
    expect(
      (second.get('input[type="password"]').element as HTMLInputElement).value,
    ).toBe("");
    expect(second.text()).toContain("重试原连接测试");
    expect(
      second
        .findAll("button")
        .find((button) => button.text() === "保存连接")!
        .attributes("disabled"),
    ).toBeDefined();
    await second.get('input[type="password"]').setValue("candidate-secret");
    await second
      .findAll("button")
      .find((button) => button.text() === "重试原连接测试")!
      .trigger("click");
    await vi.waitFor(() => expect(second.text()).toContain("连接测试未成功"));
    expect(fetch.mock.calls[0]![1].headers["Idempotency-Key"]).toBe(
      fetch.mock.calls[1]![1].headers["Idempotency-Key"],
    );
    expect(useWorkbench().provider).toEqual(provider);
    expect(sessionStorage.length).toBe(0);
    second.unmount();
  });

  it("locks the loading form so a delayed saved configuration cannot overwrite a test draft", async () => {
    let finish!: (value: typeof provider) => void;
    vi.spyOn(api, "provider").mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const test = vi.spyOn(probeApi, "test");
    const wrapper = mountSettings();
    expect(wrapper.get("fieldset").attributes("disabled")).toBeDefined();
    expect(test).not.toHaveBeenCalled();
    finish(provider);
    await flushPromises();
    expect(wrapper.get("fieldset").attributes("disabled")).toBeUndefined();
    wrapper.unmount();
  });

  it("keeps an in-flight probe owned across page changes and delivers its original result", async () => {
    let finish!: (response: Response) => void;
    const fetch = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const first = mountSettings();
    await flushPromises();
    await first.get('input[type="password"]').setValue("candidate-secret");
    await first
      .findAll("button")
      .find((button) => button.text() === "测试连接")!
      .trigger("click");
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    first.unmount();
    const second = mountSettings();
    await flushPromises();
    expect(second.get("fieldset").attributes("disabled")).toBeDefined();
    expect(second.text()).toContain("测试中…");
    finish(Response.json(result));
    await vi.waitFor(() => expect(second.text()).toContain("已恢复停止记录"));
    expect(second.get("fieldset").attributes("disabled")).toBeUndefined();
    expect(second.text()).not.toContain("重试原连接测试");
    expect(second.text()).toContain("probe-fixture");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(sessionStorage.length).toBe(0);
    second.unmount();
  });
});
