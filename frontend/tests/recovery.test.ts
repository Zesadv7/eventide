import { afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import RecoveryPanel from "../src/components/RecoveryPanel.vue";

afterEach(() => vi.unstubAllGlobals());

function mountPanel(props: { runId: string }) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/runs/:runId/recovery", component: RecoveryPanel },
      { path: "/runs/:runId/diagnostics", component: RecoveryPanel },
      { path: "/", component: RecoveryPanel },
    ],
  });
  return mount(RecoveryPanel, {
    props,
    global: { plugins: [router] },
  });
}

const base = {
  projection_version: 1,
  run_id: "r-old",
  session_id: "s-1",
  status: "interrupted",
  is_terminal: true,
  high_water: 9,
  continuation_of: null,
  summary: { steps: 3, tool_calls: 2, checkpoint_seq: 4, usage: {} },
  model_retry: null,
  stop: {
    kind: "host_restart",
    reason: "missing_post_tool_checkpoint",
    error: null,
  },
  unknown_tools: [],
  pending_approvals: [],
  recovery: {
    frozen_policy: { auto_resume: true, version: 1 },
    history_available: true,
    recovery_of: null,
    claims: [],
    successor_run_id: null,
    blocked: [],
    successor: null,
  },
  available_actions: {
    stop: {
      available: false,
      method: "POST",
      path: "/api/runs/r-old/cancel",
      reason: "run_already_terminal",
    },
    continue: {
      available: false,
      method: "POST",
      path: "/api/sessions/s-1/continue",
      reason: "not_an_interrupted_latest_run",
    },
  },
  notes: [],
};

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

it("renders facts and successor links without any write request", async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL) =>
    jsonResponse({
      ...base,
      stop: {
        kind: "auto_recovered",
        reason: "host_recovered_automatically",
        error: null,
      },
      recovery: {
        ...base.recovery,
        claims: [
          { resumed_as: "r-new", verdict: "safe_committed_boundary", at: 5 },
        ],
        successor_run_id: "r-new",
        successor: {
          run_id: "r-new",
          admitted: true,
          current: { run_id: "r-new", status: "completed", steps: 2 },
        },
      },
    }),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mountPanel({ runId: "r-old" });
  await flushPromises();
  expect(String(fetcher.mock.calls[0]?.[0])).toBe("/api/runs/r-old/recovery");
  const text = wrapper.text();
  expect(text).toContain("已领取自动续跑任务");
  expect(text).toContain("3 个模型步骤");
  expect(text).toContain("唯一后继 r-new");
  const hrefs = wrapper.findAll("a").map((a) => a.attributes("href"));
  expect(hrefs).toContain("/runs/r-new/recovery");
  expect(hrefs).toContain("/runs/r-new/diagnostics");
  // Terminal source run: neither stop nor continue, and no retry action exists.
  expect(wrapper.find('[data-testid="stop-run"]').exists()).toBe(false);
  expect(wrapper.find('[data-testid="continue-run"]').exists()).toBe(false);
  expect(wrapper.text()).toContain("没有可用操作");
  expect(
    fetcher.mock.calls.every(([url]) =>
      String(url).includes("/api/runs/r-old/recovery"),
    ),
  ).toBe(true);
  wrapper.unmount();
});

it("shows no auto-retry button for user stop and budget parks", async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL) =>
    jsonResponse({
      ...base,
      stop: {
        kind: "budget",
        reason: "token_budget",
        error: "Token budget reached",
      },
      available_actions: {
        stop: {
          available: false,
          method: "POST",
          path: "/api/runs/r-old/cancel",
          reason: "run_already_terminal",
        },
        continue: {
          available: true,
          method: "POST",
          path: "/api/sessions/s-1/continue",
          reason: "",
        },
      },
    }),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mountPanel({ runId: "r-old" });
  await flushPromises();
  expect(wrapper.text()).toContain("预算停驻");
  expect(wrapper.find('[data-testid="stop-run"]').exists()).toBe(false);
  // Manual Continue is the existing controlled endpoint; there is still no
  // automatic retry action of any kind on the page.
  const buttonLabels = wrapper
    .findAll("button")
    .map((b) => b.text())
    .join();
  expect(buttonLabels).not.toContain("重试");
  expect(buttonLabels).not.toContain("忽略");
  expect(wrapper.find('[data-testid="continue-run"]').exists()).toBe(true);
  wrapper.unmount();
});

it("keeps unknown outcomes non-recoverable and lists them", async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL) =>
    jsonResponse({
      ...base,
      stop: {
        kind: "unknown_outcome",
        reason: "unknown_tool_outcome",
        error: null,
      },
      unknown_tools: [
        { call_id: "c1", name: "bash", error: "cleanup unconfirmed", at: 7 },
      ],
      available_actions: {
        stop: {
          available: false,
          method: "POST",
          path: "/api/runs/r-old/cancel",
          reason: "run_already_terminal",
        },
        continue: {
          available: false,
          method: "POST",
          path: "/api/sessions/s-1/continue",
          reason: "unknown_tool_outcome_requires_manual_review",
        },
      },
    }),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mountPanel({ runId: "r-old" });
  await flushPromises();
  expect(wrapper.get('[data-testid="unknown-tools"]').text()).toContain("bash");
  expect(wrapper.text()).toContain(
    "unknown_tool_outcome_requires_manual_review",
  );
  expect(wrapper.find('[data-testid="continue-run"]').exists()).toBe(false);
  wrapper.unmount();
});

it("offers stop only while the server says the run can stop, then refreshes", async () => {
  const view = {
    ...base,
    status: "running",
    is_terminal: false,
    stop: { kind: "running", reason: null, error: null },
    available_actions: {
      stop: {
        available: true,
        method: "POST",
        path: "/api/runs/r-old/cancel",
        reason: "",
      },
      continue: {
        available: false,
        method: "POST",
        path: "/api/sessions/s-1/continue",
        reason: "not_an_interrupted_latest_run",
      },
    },
  };
  const fetcher = vi.fn(async (input: RequestInfo | URL) =>
    String(input).endsWith("/cancel")
      ? jsonResponse({ ok: true })
      : jsonResponse(view),
  );
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mountPanel({ runId: "r-old" });
  await flushPromises();
  expect(wrapper.find('[data-testid="stop-run"]').exists()).toBe(true);
  expect(wrapper.find('[data-testid="continue-run"]').exists()).toBe(false);
  await wrapper.get('[data-testid="stop-run"]').trigger("click");
  await flushPromises();
  await vi.waitFor(() => {
    expect(
      fetcher.mock.calls.filter(([url]) => String(url).includes("/recovery"))
        .length,
    ).toBeGreaterThanOrEqual(2);
  });
  const cancelCall = fetcher.mock.calls.find(([url]) =>
    String(url).endsWith("/cancel"),
  );
  expect(cancelCall).toBeTruthy();
  expect(
    (cancelCall as unknown as [RequestInfo | URL, RequestInit?])?.[1]?.method,
  ).toBe("POST");
  // The panel re-reads facts after the action instead of guessing the result.
  expect(
    fetcher.mock.calls.filter(([url]) => String(url).includes("/recovery"))
      .length,
  ).toBeGreaterThanOrEqual(2);
  wrapper.unmount();
});

it("ignores a late response after the run id changed", async () => {
  let releaseFirst: ((value: Response) => void) | null = null;
  const first = new Promise<Response>((resolve) => {
    releaseFirst = resolve;
  });
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).includes("r-old")) return first;
    return jsonResponse({ ...base, run_id: "r-new" });
  });
  vi.stubGlobal("fetch", fetcher);
  const wrapper = mountPanel({ runId: "r-old" });
  await flushPromises();
  await wrapper.setProps({ runId: "r-new" });
  await flushPromises();
  (releaseFirst as unknown as ((value: Response) => void) | null)?.(
    jsonResponse(base),
  );
  await flushPromises();
  expect(wrapper.text()).not.toContain("r-old");
  expect(wrapper.text()).toContain("高水位 9");
  wrapper.unmount();
});

it("retries an uncertain Continue with the same key and the original run binding", async () => {
  sessionStorage.clear();
  const writes: RequestInit[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        writes.push(init);
        if (writes.length === 1)
          throw new Error("connection lost after admission");
        return jsonResponse(
          { run_id: "resumed", session_id: "s-1", status: "accepted" },
          202,
        );
      }
      return jsonResponse({
        ...base,
        available_actions: {
          ...base.available_actions,
          continue: { ...base.available_actions.continue, available: true },
        },
      });
    }),
  );
  const wrapper = mountPanel({ runId: "r-old" });
  await flushPromises();
  await wrapper.get('[data-testid="continue-run"]').trigger("click");
  await vi.waitFor(() =>
    expect(wrapper.get('[role="alert"]').text()).toContain("connection lost"),
  );
  await wrapper.get('[data-testid="continue-run"]').trigger("click");
  await vi.waitFor(() => expect(writes).toHaveLength(2));
  await flushPromises();
  expect(writes[0]?.body).toBe(JSON.stringify({ expected_run_id: "r-old" }));
  expect(new Headers(writes[0]?.headers).get("Idempotency-Key")).toBe(
    new Headers(writes[1]?.headers).get("Idempotency-Key"),
  );
  wrapper.unmount();
  sessionStorage.clear();
});
