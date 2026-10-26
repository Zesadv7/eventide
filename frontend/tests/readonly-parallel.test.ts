import { expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import ToolActivity from "../src/components/ToolActivity.vue";

it("shows independent queued, running and cancelled members with stable batch positions", async () => {
  const batch = { batch_id: "s1b1", size: 3, parallel: true };
  const wrapper = mount(ToolActivity, {
    props: {
      tools: {
        first: {
          name: "read_file",
          status: "running",
          batch: { ...batch, position: 0 },
        },
        second: {
          name: "glob",
          status: "prepared",
          batch: { ...batch, position: 1 },
        },
        third: {
          name: "search_text",
          status: "abandoned",
          dispatched: false,
          is_error: true,
          batch: { ...batch, position: 2 },
        },
      },
    },
  });
  expect(wrapper.findAll(".tool-entry small").map((s) => s.text())).toEqual([
    "执行中",
    "等待执行",
    "未执行",
  ]);
  expect(wrapper.text()).toContain("第 1 / 3 项");
  expect(wrapper.text()).toContain("全服务最多同时执行 4 项");
  await wrapper.setProps({
    tools: { first: { name: "read_file", status: "completed" } },
  });
  expect(wrapper.text()).not.toContain("只读批次");
  expect(wrapper.get(".tool-entry small").text()).toBe("已完成");
  wrapper.unmount();
});
