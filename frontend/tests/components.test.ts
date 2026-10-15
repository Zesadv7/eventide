import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SafeContent from "../src/components/SafeContent.vue";
import GenerationEditor from "../src/components/GenerationEditor.vue";
import SettingsView from "../src/views/SettingsView.vue";
import UsageView from "../src/views/UsageView.vue";
import { api } from "../src/api/client";
import { useWorkbench } from "../src/stores/workbench";
import { provider, generation, ledger } from "./fixtures";
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
    );
    expect(
      JSON.stringify({ ...sessionStorage, ...localStorage }),
    ).not.toContain("secret-key-fixture");
    expect(wrapper.text()).not.toContain("secret-key-fixture");
  });
  it("shows unknown usage rather than inventing zero tokens or a price", async () => {
    vi.spyOn(api, "usage").mockResolvedValue(ledger);
    const store = useWorkbench();
    store.selectedSession = "session-1";
    const wrapper = mount(UsageView);
    await flushPromises();
    expect(wrapper.text()).toContain("未报告");
    expect(wrapper.text()).toContain("统计不完整");
    expect(wrapper.text()).toContain("未知");
    expect(wrapper.text()).not.toContain("0 元");
    wrapper.unmount();
  });
});
