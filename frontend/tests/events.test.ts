import { afterEach, describe, expect, it, vi } from "vitest";
import { connectRun } from "../src/api/events";
class Source {
  static current: Source;
  listeners: Record<string, (event: { data: string }) => void> = {};
  url: string;
  closed = false;
  onerror: (() => void) | null = null;
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
afterEach(() => vi.unstubAllGlobals());
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
