import { afterEach, describe, expect, it, vi } from "vitest";
import { connectRun } from "../src/api/events";
class Source {
  static current: Source;
  listeners: Record<string, (event: { data: string }) => void> = {};
  url: string;
  closed = false;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    Source.current = this;
  }
  addEventListener(name: string, callback: (event: { data: string }) => void) {
    this.listeners[name] = callback;
  }
  close() {
    this.closed = true;
  }
  emit(name: string, value: unknown) {
    this.listeners[name]?.({ data: JSON.stringify(value) });
  }
}
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
describe("SSE contract", () => {
  it("keeps previews separate from persisted sequence events", () => {
    vi.stubGlobal("EventSource", Source);
    const event = vi.fn(),
      preview = vi.fn(),
      error = vi.fn();
    const connection = connectRun("run-1", 5, event, preview, error);
    expect(Source.current.url).toContain("after=5&include_preview=true");
    Source.current.emit("model.preview", {
      type: "model.preview",
      run_id: "run-1",
      active: true,
      ephemeral: true,
      text: "临时内容",
      revision: 3,
    });
    expect(preview).toHaveBeenCalledOnce();
    expect(event).not.toHaveBeenCalled();
    Source.current.emit("model.response", {
      type: "model.response",
      run_id: "run-1",
      seq: 6,
      payload: {},
    });
    expect(event).toHaveBeenCalledOnce();
    connection.close();
    expect(Source.current.closed).toBe(true);
  });
  it("ignores mismatched identities, invalid cursors, and non-ephemeral preview payloads", () => {
    vi.stubGlobal("EventSource", Source);
    const event = vi.fn(),
      preview = vi.fn();
    connectRun("run-1", 5, event, preview, vi.fn());
    Source.current.emit("model.response", { run_id: "run-2", seq: 6 });
    Source.current.emit("model.response", { run_id: "run-1", seq: 5 });
    Source.current.emit("model.response", { run_id: "run-1", seq: "6" });
    Source.current.emit("model.preview", {
      type: "model.preview",
      run_id: "run-1",
      active: true,
      text: "invalid",
    });
    expect(event).not.toHaveBeenCalled();
    expect(preview).not.toHaveBeenCalled();
  });
});

it("treats retrieval status as a persistent event with the existing run cursor", () => {
  vi.stubGlobal("EventSource", Source);
  const event = vi.fn(),
    preview = vi.fn();
  connectRun("run-1", 5, event, preview, vi.fn());
  const payload = {
    type: "retrieval.status",
    run_id: "run-1",
    seq: 6,
    payload: { status: "preparing", workspace_id: "writer" },
  };
  Source.current.emit("retrieval.status", payload);
  expect(event).toHaveBeenCalledWith(payload);
  Source.current.emit("retrieval.status", { ...payload, seq: 5 });
  Source.current.emit("retrieval.status", { ...payload, run_id: "other" });
  expect(event).toHaveBeenCalledOnce();
  expect(preview).not.toHaveBeenCalled();
});

describe("connection recovery", () => {
  it("reconnects from the newest committed cursor and rejects old-socket events", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    vi.stubGlobal("EventSource", Source);
    const event = vi.fn(),
      preview = vi.fn(),
      status = vi.fn();
    const connection = connectRun("run-1", 5, event, preview, vi.fn(), status);
    const old = Source.current;
    old.onopen?.();
    old.emit("model.response", {
      type: "model.response",
      run_id: "run-1",
      seq: 8,
      payload: {},
    });
    old.emit("model.response", {
      type: "model.response",
      run_id: "run-1",
      seq: 8,
      payload: {},
    });
    expect(event).toHaveBeenCalledOnce();
    old.onerror?.();
    expect(old.closed).toBe(true);
    expect(status.mock.calls.at(-1)?.[0].state).toBe("retrying");
    vi.advanceTimersByTime(500);
    expect(Source.current.url).toContain("after=8&");
    old.emit("model.preview", {
      type: "model.preview",
      run_id: "run-1",
      ephemeral: true,
      active: true,
      text: "stale",
    });
    old.emit("model.response", {
      type: "model.response",
      run_id: "run-1",
      seq: 9,
      payload: {},
    });
    expect(preview).not.toHaveBeenCalled();
    expect(event).toHaveBeenCalledOnce();
    Source.current.emit("model.response", {
      type: "model.response",
      run_id: "run-1",
      seq: 10,
      payload: {},
    });
    expect(event).toHaveBeenCalledTimes(2);
    connection.close();
  });
  it("waits while offline, wakes on connectivity and stops all reconnects when closed", () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", Source);
    const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    const status = vi.fn();
    const connection = connectRun(
      "run-1",
      0,
      vi.fn(),
      vi.fn(),
      vi.fn(),
      status,
    );
    const old = Source.current;
    online.mockReturnValue(false);
    window.dispatchEvent(new Event("offline"));
    expect(old.closed).toBe(true);
    expect(status.mock.calls.at(-1)?.[0].state).toBe("offline");
    vi.advanceTimersByTime(60_000);
    expect(Source.current).toBe(old);
    online.mockReturnValue(true);
    window.dispatchEvent(new Event("online"));
    const resumed = Source.current;
    expect(resumed).not.toBe(old);
    resumed.onerror?.();
    connection.close();
    vi.advanceTimersByTime(60_000);
    window.dispatchEvent(new Event("online"));
    expect(Source.current).toBe(resumed);
  });
  it("backs off unstable connections and resets after a stable interval", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    vi.stubGlobal("EventSource", Source);
    const status = vi.fn();
    const connection = connectRun(
      "run-1",
      0,
      vi.fn(),
      vi.fn(),
      vi.fn(),
      status,
    );
    Source.current.onerror?.();
    vi.advanceTimersByTime(500);
    Source.current.onopen?.();
    Source.current.onerror?.();
    expect(status.mock.calls.at(-1)?.[0].attempt).toBe(2);
    vi.advanceTimersByTime(1000);
    Source.current.onopen?.();
    vi.advanceTimersByTime(10_000);
    Source.current.onerror?.();
    expect(status.mock.calls.at(-1)?.[0].attempt).toBe(1);
    connection.close();
  });
  it("closes a terminal run without scheduling another connection", () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", Source);
    const event = vi.fn();
    connectRun("run-1", 0, event, vi.fn(), vi.fn());
    const source = Source.current;
    source.emit("run.completed", {
      type: "run.completed",
      run_id: "run-1",
      seq: 4,
      payload: {},
    });
    expect(event).toHaveBeenCalledOnce();
    expect(source.closed).toBe(true);
    source.onerror?.();
    vi.advanceTimersByTime(60_000);
    expect(Source.current).toBe(source);
  });
});
