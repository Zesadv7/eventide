import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import UsageInsights from "../src/components/UsageInsights.vue";
import type { UsageCall, UsageLedger } from "../src/api/types";
import { ledger } from "./fixtures";

function call(run: string, patch: Partial<UsageCall> = {}): UsageCall {
  return { ...ledger.calls[0]!, run_id: run, ...patch };
}
function view(calls: UsageCall[]) {
  const data: UsageLedger = {
    ...ledger,
    calls,
    attempt_count: calls.length,
    incomplete_attempts: calls.filter((row) => !row.usage_complete).length,
  };
  return mount(UsageInsights, { props: { ledger: data } });
}
describe("whole-scope usage observations", () => {
  it("ranks only reported input/output, retains partial totals and excludes estimates", () => {
    const wrapper = view([
      call("main", {
        usage_source: "reported",
        usage_complete: true,
        usage: {
          input_tokens: 100,
          output_tokens: 50,
          cache_read_input_tokens: 100,
          reasoning_tokens: 50,
        },
      }),
      call("main", {
        usage_source: "reported",
        usage_complete: false,
        usage: { input_tokens: 20 },
      }),
      call("child", {
        usage_source: "reported",
        usage_complete: true,
        usage: { input_tokens: 200, output_tokens: 0 },
      }),
      call("estimated", {
        usage_source: "estimated",
        usage: { input_tokens: 5000 },
      }),
      call("unknown"),
    ]);
    const rows = wrapper.findAll("tbody tr");
    expect(rows).toHaveLength(2);
    expect(rows[0]!.text()).toContain("child");
    expect(rows[1]!.findAll("td").map((td) => td.text())).toEqual([
      "main",
      "170",
      "2",
      "1",
    ]);
    expect(wrapper.text()).toContain("2 个运行没有已报告总量");
    expect(wrapper.text()).not.toContain("5,000");
  });
  it("includes real zero, covers late calls and limits the ranking to five runs", () => {
    const wrapper = view(
      Array.from({ length: 7 }, (_, i) =>
        call(`run-${i}`, {
          usage_source: "reported",
          usage_complete: true,
          usage: { input_tokens: i, output_tokens: 0 },
        }),
      ),
    );
    const rows = wrapper.findAll("tbody tr");
    expect(rows).toHaveLength(5);
    expect(rows[0]!.text()).toContain("run-6");
    const zero = view([
      call("zero", {
        usage_source: "reported",
        usage_complete: true,
        usage: { input_tokens: 0, output_tokens: 0 },
      }),
    ]);
    expect(zero.get("tbody tr").findAll("td")[1]!.text()).toBe("0");
    expect(zero.text()).not.toContain("没有已报告总量");
  });
  it("counts summary attempts and repeated failures without treating cancellation as failure", () => {
    const wrapper = view([
      call("retry", { status: "failed", kind: "context_summary" }),
      call("retry", { status: "failed" }),
      call("retry", { status: "completed" }),
      call("cancelled", { status: "cancelled" }),
      call("cancelled", { status: "cancelled" }),
      call("single", { status: "failed" }),
    ]);
    expect(wrapper.text()).toContain("摘要调用 1 / 全部尝试 6");
    const failures = wrapper.get(".usage-failures");
    expect(failures.findAll("li")).toHaveLength(1);
    expect(failures.get("li").text()).toBe("retry：2 次失败 / 3 次尝试");
    expect(wrapper.find(".usage-ranking").exists()).toBe(false);
    expect(wrapper.text()).toContain("尚无可排序的已报告 Token");
  });
  it("refresh replaces the projection rather than adding its totals again", async () => {
    const calls = [
      call("a", { usage_source: "reported", usage: { input_tokens: 50 } }),
    ];
    const wrapper = view(calls);
    await wrapper.setProps({ ledger: { ...ledger, calls: [...calls] } });
    expect(wrapper.get("tbody tr").findAll("td")[1]!.text()).toBe("50");
    await wrapper.setProps({ ledger: { ...ledger, calls: [] } });
    expect(wrapper.find(".usage-ranking").exists()).toBe(false);
  });
});
