import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SafeContent from "../src/components/SafeContent.vue";
import GenerationEditor from "../src/components/GenerationEditor.vue";
import SettingsView from "../src/views/SettingsView.vue";
import UsageView from "../src/views/UsageView.vue";
import { api } from "../src/api/client";
import { budgetsApi } from "../src/api/budgets";
import { useWorkbench } from "../src/stores/workbench";
import { provider, generation, costs } from "./fixtures";
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  setActivePinia(createPinia());
});
describe("content and generation controls", () => {
  it("renders raw HTML, javascript links and script content as inert text", () => {
    const wrapper = mount(SafeContent, {
      props: {
        text: "<img src=x onerror=alert(1)>\n<script>alert(1)</script>\n[link](javascript:alert(1))\n```html\n<b>literal</b>\n```",
      },
    });
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.find("a").exists()).toBe(false);
    expect(wrapper.text()).toContain("<img");
    expect(wrapper.find("code").text()).toContain("<b>literal</b>");
  });
  it("preserves explicit numeric zero and explicit null reset", async () => {
    vi.spyOn(api, "generation").mockResolvedValue(generation);
    const wrapper = mount(GenerationEditor, {
      props: { model: "fixture-model" },
    });
    await flushPromises();
    await wrapper.find("#mode-run-temperature").setValue("value");
    await wrapper.find('input[aria-label="温度"]').setValue("0");
    expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({ temperature: 0 });
    await wrapper.find("#mode-run-temperature").setValue("default");
    expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({
      temperature: null,
    });
  });
  it("disables unsupported custom fields instead of pretending they work", async () => {
    vi.spyOn(api, "generation").mockResolvedValue({
      ...generation,
      capabilities: {
        source: "unknown",
        temperature_max: null,
        top_p: false,
        reasoning_efforts: [],
        thinking: false,
      },
    });
    const wrapper = mount(GenerationEditor, { props: { model: "unknown" } });
    await flushPromises();
    expect(
      wrapper
        .find('#mode-run-temperature option[value="value"]')
        .attributes("disabled"),
    ).toBeDefined();
    expect(
      wrapper
        .find('#mode-run-max_output_tokens option[value="value"]')
        .attributes("disabled"),
    ).toBeUndefined();
  });
  it("never persists the API key and clears it after saving", async () => {
    vi.spyOn(api, "provider").mockResolvedValue(provider);
    vi.spyOn(api, "saveProvider").mockResolvedValue(provider);
    const wrapper = mount(SettingsView, {
      global: { stubs: { GenerationEditor: true } },
    });
    await flushPromises();
    await wrapper.find('input[type="password"]').setValue("secret-key-fixture");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(
      (wrapper.find('input[type="password"]').element as HTMLInputElement)
        .value,
    ).toBe("");
    expect(api.saveProvider).toHaveBeenCalledWith(
      expect.objectContaining({ api_key: "secret-key-fixture" }),
      provider.config_hash,
    );
    expect(
      JSON.stringify({ ...sessionStorage, ...localStorage }),
    ).not.toContain("secret-key-fixture");
    expect(wrapper.text()).not.toContain("secret-key-fixture");
  });
  it("shows observed request metadata including zero latency without fabricating token usage", async () => {
    vi.spyOn(budgetsApi, "costs").mockResolvedValue({
      ...costs,
      usage: {
        ...costs.usage,
        calls: [
          {
            ...costs.usage.calls[0]!,
            provider: "openai_responses",
            duration_ms: 0,
            stop_reason: "length",
          },
        ],
      },
    });
    vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
    useWorkbench().selectedSession = "session-1";
    const wrapper = mount(UsageView);
    await flushPromises();
    expect(wrapper.get(".call-details").text()).toContain("openai_responses");
    expect(wrapper.get(".call-details").text()).toContain("length");
    expect(wrapper.get(".call-details").text()).toContain("0 ms");
    expect(wrapper.get(".call-details").text()).toContain("未报告");
    wrapper.unmount();
  });
  it("labels input-only embedding output as inapplicable rather than unknown or zero", async () => {
    vi.spyOn(budgetsApi, "costs").mockResolvedValue({
      ...costs,
      usage: {
        ...costs.usage,
        calls: [
          {
            ...costs.usage.calls[0]!,
            kind: "embedding",
            usage_basis: "input_only",
            usage: { input_tokens: 7 },
            usage_source: "reported",
            usage_complete: true,
          },
        ],
      },
    });
    vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
    useWorkbench().selectedSession = "session-1";
    const wrapper = mount(UsageView);
    await flushPromises();
    expect(wrapper.text()).toContain("不适用（仅输入）");
    wrapper.unmount();
  });
  it("shows unknown usage rather than inventing zero tokens or a price", async () => {
    vi.spyOn(budgetsApi, "costs").mockResolvedValue(costs);
    vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
    const store = useWorkbench();
    store.selectedSession = "session-1";
    const wrapper = mount(UsageView);
    await flushPromises();
    expect(wrapper.text()).toContain("未报告");
    expect(wrapper.text()).toContain("统计不完整");
    expect(wrapper.text()).toContain("未知");
    expect(wrapper.text()).not.toContain("0 元");
    expect(wrapper.text()).toContain("未记录供应商");
    expect(wrapper.text()).toContain("未记录结束原因");
    expect(wrapper.text()).not.toContain("0 ms");
    expect(wrapper.text()).toContain("输入组成未记录");
    expect(wrapper.find(".input-composition").exists()).toBe(false);
    wrapper.unmount();
  });
  it("keeps input character composition separate from unknown provider tokens", async () => {
    vi.spyOn(budgetsApi, "costs").mockResolvedValue({
      ...costs,
      usage: {
        ...costs.usage,
        calls: [
          {
            ...costs.usage.calls[0]!,
            input_composition: {
              method: "serialized_unicode_characters_v1",
              unit: "characters",
              total_characters: 1234,
              components: {
                instructions: 100,
                history: 800,
                tool_catalog: 200,
                skills: 30,
                retrieval: 0,
                tool_results: 50,
                envelope: 54,
              },
            },
          },
        ],
      },
    });
    vi.spyOn(budgetsApi, "prices").mockResolvedValue({ prices: [] });
    useWorkbench().selectedSession = "session-1";
    const wrapper = mount(UsageView);
    await flushPromises();
    const composition = wrapper.get(".input-composition");
    expect(composition.get("summary").text()).toBe("输入组成 · 1,234 字符");
    expect(composition.text()).toContain("0 字符");
    expect(composition.text()).toContain("非 Token 数");
    expect(composition.text()).toContain("已压缩的摘要计入对话历史");
    expect(wrapper.get(".call-details").text()).toContain("未报告");
    expect(wrapper.get(".stat-grid").text()).toContain("统计不完整");
    wrapper.unmount();
  });
});
