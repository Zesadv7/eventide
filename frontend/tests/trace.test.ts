import { afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import RunTrace from "../src/components/RunTrace.vue";
const report = {
  schema_version: 1,
  source: "eventide.event_log",
  spans: [
    {
      name: "eventide.model",
      trace_id: "a".repeat(32),
      span_id: "b".repeat(16),
      parent_span_id: null,
      start_time_ns: 1000000,
      end_time_ns: 3000000,
      status: "completed",
      attributes: { "eventide.duration_ms": 2 },
      links: [],
    },
  ],
};
afterEach(() => vi.unstubAllGlobals());
it("loads only on request and offers a redacted download", async () => {
  const fetcher = vi.fn(
    async (_url: RequestInfo | URL) =>
      new Response(JSON.stringify(report), { status: 200 }),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mount(RunTrace, { props: { runId: "run/one" } });
  expect(fetcher).not.toHaveBeenCalled();
  await wrapper.get("button").trigger("click");
  await flushPromises();
  expect(fetcher.mock.calls[0][0]).toBe("/api/runs/run%2Fone/trace");
  expect(wrapper.text()).toContain("模型请求 · completed · 2.0 ms");
  expect(wrapper.get("a").attributes("href")).toBe(
    "/api/runs/run%2Fone/trace?download=true",
  );
  wrapper.unmount();
});
it("drops an old Run response after switching identity", async () => {
  let finish!: (value: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise((r) => {
          finish = r;
        }),
    ),
  );
  const wrapper = mount(RunTrace, { props: { runId: "old" } });
  await wrapper.get("button").trigger("click");
  await wrapper.setProps({ runId: "new" });
  finish(new Response(JSON.stringify(report), { status: 200 }));
  await flushPromises();
  expect(wrapper.find("a").exists()).toBe(false);
  expect(wrapper.find("ol").exists()).toBe(false);
  expect(wrapper.get("button").text()).toBe("查看 Trace");
  wrapper.unmount();
});
it("shows missing evidence errors without fabricating spans", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            code: "conflict",
            message: "Trace超过关联记录限制",
            detail: "Trace超过关联记录限制",
          }),
          { status: 409 },
        ),
    ),
  );
  const wrapper = mount(RunTrace, { props: { runId: "too-many" } });
  await wrapper.get("button").trigger("click");
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain(
    "Trace超过关联记录限制",
  );
  expect(wrapper.find("ol").exists()).toBe(false);
  wrapper.unmount();
});
