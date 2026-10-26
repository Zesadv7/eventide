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
import type { GenerationState } from "../src/api/types";
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
  it("uses GLM declared sampling and thinking limits in the form", async () => {
    vi.spyOn(api, "generation").mockResolvedValue({
      ...generation,
      capabilities: {
        source: "documented",
        temperature_max: 1,
        top_p: true,
        reasoning_efforts: ["low", "high", "max"],
        response_formats: [],
        thinking: true,
        thinking_modes: ["enabled"],
        top_p_min: 0.01,
        sampling_precision: 2,
        max_output_tokens: 131072,
      },
    });
    const wrapper = mount(GenerationEditor, {
      props: { model: "glm-5.3-flash" },
    });
    await flushPromises();
    await wrapper.find("#mode-run-thinking").setValue("value");
    expect(
      wrapper
        .find('select[aria-label="思考模式值"] option[value="disabled"]')
        .exists(),
    ).toBe(false);
    await wrapper.find("#mode-run-temperature").setValue("value");
    expect(wrapper.find('input[aria-label="温度"]').attributes("max")).toBe(
      "1",
    );
    expect(wrapper.find('input[aria-label="温度"]').attributes("step")).toBe(
      "0.01",
    );
    await wrapper.find("#mode-run-top_p").setValue("value");
    expect(wrapper.find('input[aria-label="Top P"]').attributes("min")).toBe(
      "0.01",
    );
    await wrapper.find("#mode-run-max_output_tokens").setValue("value");
    expect(
      wrapper.find('input[aria-label="最大输出 Token"]').attributes("max"),
    ).toBe("131072");
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
  it("emits declared text or JSON format and preserves inherit and explicit default", async () => {
    vi.spyOn(api, "generation").mockResolvedValue({
      ...generation,
      capabilities: {
        ...generation.capabilities,
        response_formats: ["text", "json_object"],
      },
    });
    const wrapper = mount(GenerationEditor, {
      props: { model: "fixture-model" },
    });
    await flushPromises();
    expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({});
    await wrapper.find("#mode-run-response_format").setValue("value");
    const choices = wrapper.find('select[aria-label="输出格式值"]');
    expect(
      choices.findAll("option").map((option) => option.attributes("value")),
    ).toEqual(["text", "json_object"]);
    await choices.setValue("json_object");
    expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({
      response_format: "json_object",
    });
    await choices.setValue("text");
    expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({
      response_format: "text",
    });
    await wrapper.find("#mode-run-response_format").setValue("default");
    expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({
      response_format: null,
    });
    await wrapper.find("#mode-run-response_format").setValue("inherit");
    expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({});
    expect(wrapper.text()).toContain("JSON 模式只保证输出格式，不保证事实正确");
    wrapper.unmount();
  });
  it.each([
    ["model", "json_object", "value"],
    ["session", null, "default"],
  ] as const)(
    "loads %s format overrides without changing their semantics",
    async (scope, value, mode) => {
      vi.spyOn(api, "generation").mockResolvedValue({
        ...generation,
        defaults: { response_format: "json_object" },
        session: { response_format: null },
        capabilities: {
          ...generation.capabilities,
          response_formats: ["text", "json_object"],
        },
      });
      const wrapper = mount(GenerationEditor, {
        props: { model: "fixture-model", session: "session-1", scope },
      });
      await flushPromises();
      expect(
        (
          wrapper.find(`#mode-${scope}-response_format`)
            .element as HTMLSelectElement
        ).value,
      ).toBe(mode);
      expect(wrapper.emitted("change")?.at(-1)?.[0]).toEqual({
        response_format: value,
      });
      wrapper.unmount();
    },
  );
  it("disables unsupported custom fields instead of pretending they work", async () => {
    vi.spyOn(api, "generation").mockResolvedValue({
      ...generation,
      capabilities: {
        source: "unknown",
        temperature_max: null,
        top_p: false,
        reasoning_efforts: [],
        response_formats: [],
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
    expect(
      wrapper
        .find('#mode-run-response_format option[value="value"]')
        .attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.find('select[aria-label="输出格式值"]').exists()).toBe(
      false,
    );
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

describe("MK-14 model capability card", () => {
  const capability = {
    identity: {
      provider: "openai_compatible",
      protocol: "openai_chat",
      canonical_endpoint: "https://open.bigmodel.cn/api/coding/paas/v4",
      model: "glm-5.3-flash",
      identity_hash: "c".repeat(64),
    },
    source: "documented" as const,
    catalog_version: "1",
    capabilities: {
      source: "documented",
      temperature_max: 1,
      top_p: true,
      reasoning_efforts: ["low", "high", "max"],
      response_formats: ["text"],
      thinking: true,
      context_window: null,
      tools_supported: null,
      streaming_supported: null,
      max_output_tokens: 131072,
    },
    preset: {
      key: "bigmodel-coding-chat/glm-5.3-flash",
      reference: "eventide/generation.py capabilities() BigModel chat 映射",
      verified_at: "2026-10-16",
      documented_url: null,
    },
    overrides: { fields: [] },
    verification: {
      config_saved: true,
      connection: {
        status: "failed" as const,
        checked_at: null,
        run_id: "probe_abc",
        error_type: "authentication",
      },
      tool_verification: { status: "not_verified" },
    },
  };
  it("shows source, unknown facts as null, and separated verification states", async () => {
    vi.spyOn(api, "generation").mockResolvedValue({
      ...generation,
      capability,
    });
    const wrapper = mount(GenerationEditor, {
      props: { model: "glm-5.3-flash" },
    });
    await flushPromises();
    const card = wrapper.get(".capability-card");
    expect(card.text()).toContain("bigmodel-coding-chat/glm-5.3-flash");
    expect(card.text()).toContain("https://open.bigmodel.cn/api/coding/paas/v4");
    expect(card.text()).toContain("目录版本 1");
    expect(card.text()).toContain("上下文 未知");
    expect(card.text()).toContain("工具 未知");
    expect(card.text()).toContain("流式 未知");
    expect(card.text()).toContain("最大输出 131072");
    expect(card.text()).toContain("配置已保存");
    expect(card.text()).toContain("连接检查 失败（authentication）");
    expect(card.text()).toContain("Run probe_abc");
    expect(card.text()).toContain("工具验证：未验证");
    wrapper.unmount();
  });
  it("keeps a stale capability card from crossing after a model switch", async () => {
    const resolvers: Array<(value: GenerationState) => void> = [];
    vi.spyOn(api, "generation").mockImplementation(
      () =>
        new Promise<GenerationState>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    const wrapper = mount(GenerationEditor, {
      props: { model: "glm-5.3-flash" },
    });
    await flushPromises();
    await wrapper.setProps({ model: "other-model" });
    await flushPromises();
    // The request issued for the previous model resolves late.
    resolvers[0]?.({
      ...generation,
      model: "glm-5.3-flash",
      capability,
    });
    await flushPromises();
    expect(wrapper.find(".capability-card").exists()).toBe(false);
    wrapper.unmount();
  });
});
