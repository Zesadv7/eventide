import { afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import CommandLogPanel from "../src/components/CommandLogPanel.vue";
import ToolActivity from "../src/components/ToolActivity.vue";

const reference = {
  artifact_id: "a".repeat(32),
  run_id: "run-1",
  call_id: "call-1",
  streams: {
    stdout: {
      received_bytes: 9000,
      stored_bytes: 9000,
      stored_chars: 9000,
      sha256: "c".repeat(64),
      sealed: true,
      encoding_loss: false,
      capture_state: "complete",
      reason: null,
    },
    stderr: {
      received_bytes: 0,
      stored_bytes: 0,
      stored_chars: 0,
      sha256: null,
      sealed: false,
      encoding_loss: false,
      capture_state: "complete",
      reason: null,
    },
  },
};

function pageResponse(available: boolean, text = "") {
  return new Response(
    JSON.stringify({
      available,
      ...(available
        ? {
            stream: "stdout",
            start: 0,
            end: text.length,
            total_chars: 9000,
            stored_bytes: 9000,
            received_bytes: 9000,
            next_offset: text.length < 9000 ? text.length : null,
            capture_state: "complete",
            reason: null,
            encoding_loss: false,
            sealed: true,
          }
        : { availability: "expired", reason: "retention" }),
      text,
    }),
    { status: 200 },
  );
}

afterEach(() => vi.unstubAllGlobals());

it("pages the log on request and shows honest capture facts", async () => {
  const fetcher = vi.fn(
    async (_url: RequestInfo | URL) => new Response(null, { status: 500 }),
  );
  fetcher.mockImplementationOnce(
    async () => new Response(JSON.stringify({}), { status: 200 }),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mount(ToolActivity, {
    props: {
      tools: { call_1: { name: "bash", metadata: { command_log: reference } } },
    },
  });
  const entry = wrapper.get(".command-log-entry");
  await entry.get("summary").trigger("click");
  const panelWrapper = wrapper.getComponent(CommandLogPanel);
  expect(fetcher).not.toHaveBeenCalled();
  await panelWrapper.findAll("button").at(2)!.trigger("click");
  await flushPromises();
  expect(fetcher.mock.calls[0][0]).toBe(
    "/api/runs/run-1/calls/call-1/command-log?stream=stdout&offset=0&limit=4000",
  );
  wrapper.unmount();
});

it("renders an available page and offers the next page", async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL) =>
    pageResponse(true, "middle-chunk"),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mount(CommandLogPanel, { props: { log: reference } });
  await wrapper.findAll("button").at(2)!.trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("middle-chunk");
  expect(wrapper.text()).toContain("字符 0–12 / 9000");
  expect(wrapper.text()).toContain("下一页");
  await wrapper.get(".paging button").trigger("click");
  await flushPromises();
  expect(fetcher.mock.calls[1][0]).toBe(
    "/api/runs/run-1/calls/call-1/command-log?stream=stdout&offset=12&limit=4000",
  );
  wrapper.unmount();
});

it("shows an explicit unavailability instead of pretending success", async () => {
  const fetcher = vi.fn(async () => pageResponse(false));
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mount(CommandLogPanel, { props: { log: reference } });
  await wrapper.findAll("button").at(2)!.trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("日志不可用：expired");
  expect(wrapper.find("pre").exists()).toBe(false);
  wrapper.unmount();
});

it("drops late stdout when stderr finished first and resets on a new identity", async () => {
  let stdout!: (value: Response) => void;
  const fetcher = vi.fn((url: RequestInfo | URL) =>
    String(url).includes("stream=stdout")
      ? new Promise<Response>((resolve) => {
          stdout = resolve;
        })
      : Promise.resolve(pageResponse(true, "stderr-current")),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mount(CommandLogPanel, { props: { log: reference } });
  await wrapper.findAll("button").at(2)!.trigger("click");
  await wrapper.findAll('[role="tab"]').at(1)!.trigger("click");
  await flushPromises();
  expect(wrapper.get("pre").text()).toBe("stderr-current");
  stdout(pageResponse(true, "stale-stdout"));
  await flushPromises();
  expect(wrapper.get("pre").text()).toBe("stderr-current");
  await wrapper.setProps({
    log: { ...reference, artifact_id: "b".repeat(32), call_id: "new-call" },
  });
  expect(wrapper.find("pre").exists()).toBe(false);
  expect(
    wrapper.findAll('[role="tab"]').at(0)!.attributes("aria-selected"),
  ).toBe("true");
  await wrapper.findAll("button").at(2)!.trigger("click");
  const late = stdout;
  wrapper.unmount();
  late(pageResponse(true, "after-unmount"));
  await flushPromises();
});
