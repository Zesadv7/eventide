import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { api, ApiError } from "../src/api/client";
import { useWorkbench } from "../src/stores/workbench";
import { session, run, provider } from "./fixtures";
import { connectRun } from "../src/api/events";
vi.mock("../src/api/events", () => ({
  connectRun: vi.fn(() => ({ close: vi.fn() })),
}));
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  vi.spyOn(api, "session").mockResolvedValue({ ...session });
  vi.spyOn(api, "sessions").mockResolvedValue([{ ...session }]);
  vi.spyOn(api, "runs").mockResolvedValue([{ ...run }]);
  vi.spyOn(api, "run").mockImplementation(async () => ({
    ...run,
    status: "running",
  }));
  vi.spyOn(api, "messages").mockResolvedValue([
    { role: "assistant", content: "已完成" },
  ]);
  vi.spyOn(api, "attachments").mockResolvedValue([]);
});
afterEach(() => {
  vi.useRealTimers();
});
const input = {
  prompt: "修复测试",
  mode: "auto",
  execution_target: "source" as const,
  generation: {},
  attachment_ids: [],
  model: null,
};
describe("request identity and UI isolation", () => {
  it("keeps source ownership exclusive while admitted snapshots can overlap", () => {
    const store = useWorkbench();
    store.selectedWorkspace = session.workspace_id;
    store.sessions[session.workspace_id] = [
      { ...session, status: "running", execution: { kind: "snapshot" } },
    ];
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "writer",
      status: "running",
      execution: { kind: "worktree" },
    });
    expect(store.workspaceBusy).toBe(false);
    store.sessions[session.workspace_id]!.push({
      ...session,
      session_id: "source",
      status: "running",
      execution: { kind: "source" },
    });
    expect(store.workspaceBusy).toBe(true);
    store.sessions[session.workspace_id]!.at(-1)!.status = "queued";
    expect(store.workspaceBusy).toBe(false);
  });
  it("keeps the same idempotency key after an ambiguous network failure", async () => {
    const submit = vi
      .spyOn(api, "submit")
      .mockRejectedValueOnce(new TypeError("断网"))
      .mockResolvedValueOnce({
        run_id: "r",
        session_id: session.session_id,
        status: "accepted",
      });
    const store = useWorkbench();
    store.selectedWorkspace = session.workspace_id;
    store.selectedSession = session.session_id;
    store.setDraft(session.session_id, input.prompt);
    await expect(store.submit(input)).rejects.toThrow("断网");
    const key = store.pending[session.session_id]!.key;
    await store.retryPending();
    expect(submit.mock.calls.map((c) => c[2])).toEqual([key, key]);
    expect(store.pending[session.session_id]).toBeUndefined();
    expect(store.drafts[session.session_id]).toBe("");
  });
  it("keeps unknown server outcomes but clears known rejected requests", async () => {
    const submit = vi
      .spyOn(api, "submit")
      .mockRejectedValueOnce(new ApiError(503, "服务中断"))
      .mockRejectedValueOnce(new ApiError(422, "参数错误"));
    const store = useWorkbench();
    store.selectedSession = session.session_id;
    await expect(store.submit(input)).rejects.toThrow();
    expect(store.pending[session.session_id]).toBeDefined();
    await expect(store.retryPending()).rejects.toThrow("参数错误");
    expect(store.pending[session.session_id]).toBeUndefined();
    expect(submit).toHaveBeenCalledTimes(2);
  });
  it("prevents editing an ambiguous request into a second task", async () => {
    vi.spyOn(api, "submit").mockRejectedValue(new TypeError("断网"));
    const store = useWorkbench();
    store.selectedSession = session.session_id;
    await expect(store.submit(input)).rejects.toThrow();
    await expect(
      store.submit({ ...input, prompt: "不同任务" }),
    ).rejects.toThrow("上次接收结果");
    expect(api.submit).toHaveBeenCalledTimes(1);
  });
  it("retains an unmarked conflict across refresh and only clears an explicit refusal", async () => {
    const submit = vi
      .spyOn(api, "submit")
      .mockRejectedValueOnce(new ApiError(409, "接收结果未知"))
      .mockRejectedValueOnce(new ApiError(409, "明确未接收", "rejected"))
      .mockResolvedValue({
        run_id: "accepted",
        session_id: session.session_id,
        status: "accepted",
      });
    let store = useWorkbench();
    store.selectedSession = session.session_id;
    await expect(store.submit(input)).rejects.toThrow("接收结果未知");
    const original = store.pending[session.session_id]!.key;
    setActivePinia(createPinia());
    store = useWorkbench();
    store.selectedSession = session.session_id;
    await expect(store.retryPending()).rejects.toThrow("明确未接收");
    expect(submit.mock.calls[1]![2]).toBe(original);
    expect(store.pending[session.session_id]).toBeUndefined();
    await store.submit(input);
    expect(submit.mock.calls[2]![2]).not.toBe(original);
  });
  it("keeps Continue identity after an unknown result and refresh, but clears a refusal", async () => {
    const resume = vi
      .spyOn(api, "continue")
      .mockRejectedValueOnce(new ApiError(503, "接收结果未知"))
      .mockRejectedValueOnce(new ApiError(409, "源码变化，未接收", "rejected"))
      .mockResolvedValue({
        run_id: "continued",
        session_id: session.session_id,
        status: "accepted",
      });
    let store = useWorkbench();
    store.selectedSession = session.session_id;
    await expect(store.continueRun()).rejects.toThrow("接收结果未知");
    const storage = `eventide.continue.${session.session_id}`;
    const original = JSON.parse(sessionStorage.getItem(storage)!);
    setActivePinia(createPinia());
    store = useWorkbench();
    store.selectedSession = session.session_id;
    await expect(store.continueRun()).rejects.toThrow("未接收");
    expect(resume.mock.calls[1]![1]).toBe(original);
    expect(sessionStorage.getItem(storage)).toBeNull();
    await store.continueRun();
    expect(resume.mock.calls[2]![1]).not.toBe(original);
  });
  it("persists separate drafts without storing provider secrets", () => {
    const store = useWorkbench();
    store.provider = provider;
    store.setDraft("a", "分析A");
    store.setDraft("b", "分析B");
    expect(JSON.parse(sessionStorage.getItem("eventide.drafts")!)).toEqual({
      a: "分析A",
      b: "分析B",
    });
    expect(sessionStorage.getItem("eventide.provider")).toBeNull();
  });
  it("does not cancel server work when switching away or disposing UI observers", async () => {
    vi.spyOn(api, "runs").mockResolvedValue([{ ...run, status: "running" }]);
    vi.spyOn(api, "cancel");
    const store = useWorkbench();
    await store.selectSession(session.session_id);
    store.dispose();
    expect(connectRun).toHaveBeenCalled();
    expect(api.cancel).not.toHaveBeenCalled();
  });
  it("does not move preview text into saved messages", async () => {
    vi.spyOn(api, "runs").mockResolvedValue([{ ...run, status: "running" }]);
    const store = useWorkbench();
    await store.selectSession(session.session_id);
    const args = vi.mocked(connectRun).mock.calls.at(-1)!;
    args[3]({
      type: "model.preview",
      run_id: run.run_id,
      text: "未保存",
      active: true,
      ephemeral: true,
      revision: 1,
    });
    expect(store.previews[run.run_id]?.text).toBe("未保存");
    expect(store.messages[session.session_id]?.[0]?.content).toBe("已完成");
    args[3]({
      type: "model.preview",
      run_id: run.run_id,
      text: "",
      active: false,
      ephemeral: true,
    });
    expect(store.previews[run.run_id]).toBeUndefined();
    store.dispose();
  });
  it("uses the last chronological Run instead of stopping an older completed Run", async () => {
    vi.spyOn(api, "runs").mockResolvedValue([
      { ...run, run_id: "old", status: "completed" },
      { ...run, run_id: "latest", status: "running" },
    ]);
    vi.spyOn(api, "run").mockImplementation(async (identity) => ({
      ...run,
      run_id: identity,
      status: "running",
    }));
    vi.spyOn(api, "cancel").mockResolvedValue({});
    const store = useWorkbench();
    await store.selectSession(session.session_id);
    expect(store.currentRun?.run_id).toBe("latest");
    await store.cancel();
    expect(api.cancel).toHaveBeenCalledWith("latest");
    store.dispose();
  });
  it("keeps approval/status refresh working while tool history is temporarily incomplete", async () => {
    vi.spyOn(api, "messages").mockRejectedValue(
      new ApiError(
        409,
        "Incomplete tool history; Continue must resolve pending calls",
      ),
    );
    vi.spyOn(api, "run").mockResolvedValue({
      ...run,
      status: "waiting_for_user",
      pending_approvals: { approval1: { approval_id: "approval1" } },
    });
    const store = useWorkbench();
    store.messages[session.session_id] = [
      { role: "user", content: "检查项目" },
    ];
    await store.selectSession(session.session_id);
    expect(store.currentRun?.pending_approvals).toHaveProperty("approval1");
    expect(store.messages[session.session_id]?.[0]?.content).toBe("检查项目");
    store.dispose();
  });
});

it("refreshes durable retrieval evidence on SSE and restores it through the same Run read", async () => {
  vi.mocked(api.runs).mockImplementation(async () => [{ ...run }]);
  const preparing = {
    status: "preparing" as const,
    workspace_id: "actual-copy",
  };
  vi.mocked(api.run).mockResolvedValue({
    ...run,
    status: "running",
    retrieval: preparing,
  });
  const store = useWorkbench();
  await store.selectSession(session.session_id);
  expect(store.currentRun?.retrieval).toEqual(preparing);
  const available = {
    status: "available" as const,
    workspace_id: "actual-copy",
    generation: 7,
    location: "managed" as const,
  };
  vi.mocked(api.run).mockResolvedValue({
    ...run,
    status: "running",
    retrieval: available,
  });
  const onEvent = vi.mocked(connectRun).mock.calls.at(-1)![2];
  onEvent({
    type: "retrieval.status",
    run_id: run.run_id,
    seq: 1,
    payload: available,
  });
  await flushPromises();
  expect(store.currentRun?.retrieval).toEqual(available);
  expect(store.events[run.run_id]?.at(-1)?.seq).toBe(1);
  store.dispose();
  setActivePinia(createPinia());
  const restored = useWorkbench();
  await restored.selectSession(session.session_id);
  expect(restored.currentRun?.retrieval).toEqual(available);
  restored.dispose();
});

it("refreshes after reconnect but ignores a detached connection's callbacks", async () => {
  const store = useWorkbench();
  await store.selectSession(session.session_id);
  const old = vi.mocked(connectRun).mock.calls.at(-1)!;
  old[3]({
    type: "model.preview",
    run_id: run.run_id,
    text: "old preview",
    active: true,
    ephemeral: true,
  });
  old[4]();
  expect(store.previews[run.run_id]).toBeUndefined();
  old[5]?.({ state: "retrying", attempt: 1, nextRetryAt: Date.now() + 1000 });
  expect(store.eventConnection.state).toBe("retrying");
  const reads = vi.mocked(api.session).mock.calls.length;
  old[5]?.({ state: "connected", attempt: 1, nextRetryAt: null });
  await flushPromises();
  expect(vi.mocked(api.session).mock.calls.length).toBeGreaterThan(reads);
  store.dispose();
  old[3]({
    type: "model.preview",
    run_id: run.run_id,
    text: "late",
    active: true,
    ephemeral: true,
  });
  old[5]?.({ state: "retrying", attempt: 2, nextRetryAt: Date.now() });
  expect(store.previews[run.run_id]).toBeUndefined();
  expect(store.eventConnection.state).toBe("closed");
});

it("does not label a retrying event connection connected just because polling works", async () => {
  vi.useFakeTimers();
  const store = useWorkbench();
  await store.selectSession(session.session_id);
  vi.mocked(connectRun).mock.calls.at(-1)![5]?.({
    state: "retrying",
    attempt: 1,
    nextRetryAt: Date.now() + 5000,
  });
  store.startPolling();
  await vi.advanceTimersByTimeAsync(2500);
  expect(store.connected).toBe(false);
  expect(store.eventConnection.state).toBe("retrying");
  store.dispose();
});
