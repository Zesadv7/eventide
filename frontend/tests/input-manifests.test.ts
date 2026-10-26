import { mount, flushPromises } from "@vue/test-utils";
import { expect, it, vi, beforeEach } from "vitest";
import InputManifestPanel from "../src/components/InputManifestPanel.vue";
import { inputManifestsApi } from "../src/api/inputManifests";
vi.mock("../src/api/inputManifests", () => ({
  inputManifestsApi: { list: vi.fn() },
}));
beforeEach(() => vi.clearAllMocks());
const empty = (id: string) => ({
  run_id: id,
  entries: [],
  next_before: null,
  recorded_count: 0,
  high_water: 1,
});
it("loads only on demand and cannot show an old run's late response", async () => {
  let old!: (value: ReturnType<typeof empty>) => void;
  vi.mocked(inputManifestsApi.list)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          old = resolve;
        }),
    )
    .mockResolvedValueOnce(empty("new"));
  const wrapper = mount(InputManifestPanel, { props: { runId: "old" } });
  expect(inputManifestsApi.list).not.toHaveBeenCalled();
  wrapper.element.setAttribute("open", "");
  await wrapper.trigger("toggle");
  await wrapper.setProps({ runId: "new" });
  await flushPromises();
  old({ ...empty("old"), high_water: 999 });
  await flushPromises();
  expect(wrapper.text()).toContain("事件位置 1");
  expect(wrapper.text()).not.toContain("999");
  expect(wrapper.text()).toContain("不会补造记录");
});
it("shows retry errors and never treats unknown inputs as empty evidence", async () => {
  vi.mocked(inputManifestsApi.list)
    .mockRejectedValueOnce(new Error("connection lost"))
    .mockResolvedValueOnce(empty("r"));
  const wrapper = mount(InputManifestPanel, { props: { runId: "r" } });
  wrapper.element.setAttribute("open", "");
  await wrapper.trigger("toggle");
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toBe("connection lost");
  expect(wrapper.text()).not.toContain("没有已记录的输入清单");
  await wrapper.get("button").trigger("click");
  await flushPromises();
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(wrapper.text()).toContain("没有已记录的输入清单");
});
