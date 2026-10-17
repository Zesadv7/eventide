import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import ProjectFolderPicker from "../src/components/ProjectFolderPicker.vue";
import * as directories from "../src/api/directories";

beforeEach(() => vi.restoreAllMocks());
it("opens the system chooser on mount and fills the exact path without registration", async () => {
  const choose = vi
    .spyOn(directories, "chooseDirectory")
    .mockResolvedValue({ status: "selected", path: "/tmp/ project \n" });
  const wrapper = mount(ProjectFolderPicker, {
    props: { modelValue: "", initiallyOpen: true },
  });
  await flushPromises();
  expect(choose).toHaveBeenCalledTimes(1);
  expect(wrapper.emitted("update:modelValue")).toEqual([["/tmp/ project \n"]]);
  expect(wrapper.find("input").exists()).toBe(false);
  wrapper.unmount();
});
it("cancel leaves the path unchanged and never opens a fallback automatically", async () => {
  vi.spyOn(directories, "chooseDirectory").mockResolvedValue({
    status: "cancelled",
  });
  const wrapper = mount(ProjectFolderPicker, {
    props: { modelValue: "/old", initiallyOpen: true },
  });
  await flushPromises();
  expect(wrapper.emitted("update:modelValue")).toBeUndefined();
  expect(wrapper.emitted("close")).toHaveLength(1);
  expect(wrapper.find("input").exists()).toBe(false);
  wrapper.unmount();
});
it("unavailable permits explicit manual entry, back and close", async () => {
  vi.spyOn(directories, "chooseDirectory").mockResolvedValue({
    status: "unavailable",
  });
  const wrapper = mount(ProjectFolderPicker, {
    props: { modelValue: "", initiallyOpen: true },
    attachTo: document.body,
  });
  await flushPromises();
  expect(wrapper.text()).toContain("无法打开系统窗口");
  expect(wrapper.find("input").exists()).toBe(false);
  await wrapper.findAll("button")[1]!.trigger("click");
  expect(document.activeElement).toBe(wrapper.find("input").element);
  await wrapper.find("input").setValue(" /manual ");
  expect(wrapper.emitted("update:modelValue")).toEqual([[" /manual "]]);
  await wrapper.find('[aria-label="返回目录选择"]').trigger("click");
  expect(wrapper.find("input").exists()).toBe(false);
  await wrapper.find('[aria-label="关闭目录选择"]').trigger("click");
  expect(wrapper.emitted("close")).toHaveLength(1);
  wrapper.unmount();
});
it("blocks duplicate launches and aborts on Escape and unmount, ignoring late responses", async () => {
  let finish!: (value: directories.DirectoryChoice) => void;
  const choose = vi.spyOn(directories, "chooseDirectory").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = mount(ProjectFolderPicker, {
    props: { modelValue: "", initiallyOpen: true },
  });
  await wrapper.find("button").trigger("click");
  expect(choose).toHaveBeenCalledTimes(1);
  const signal = choose.mock.calls[0]![0];
  await wrapper.trigger("keydown", { key: "Escape" });
  expect(signal.aborted).toBe(true);
  finish({ status: "selected", path: "/late" });
  await flushPromises();
  expect(wrapper.emitted("update:modelValue")).toBeUndefined();
  wrapper.unmount();
});
it("API uses the caller cancellation signal instead of the normal 30 second timeout", async () => {
  const fetcher = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(
      new Response(JSON.stringify({ status: "cancelled" }), { status: 200 }),
    );
  const controller = new AbortController();
  await directories.chooseDirectory(controller.signal);
  expect(fetcher).toHaveBeenCalledWith(
    "/api/directories/choose",
    expect.objectContaining({ method: "POST", signal: controller.signal }),
  );
});
