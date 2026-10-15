import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
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
  generation: {},
  attachment_ids: [],
  model: null,
};
describe("request identity and UI isolation", () => {
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
