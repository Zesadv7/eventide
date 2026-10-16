import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/api/client";
afterEach(() => vi.unstubAllGlobals());
describe("HTTP identity contract", () => {
  it("normalizes the backend id while preserving precise session ownership", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify([
              { id: "run-real", session_id: "session-real", status: "running" },
            ]),
            { status: 200 },
          ),
        ),
    );
    const result = await api.runs("session-real");
    expect(result[0]).toMatchObject({
      run_id: "run-real",
      session_id: "session-real",
    });
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe(
      "/api/sessions/session-real/runs?limit=100",
    );
  });
  it("rejects a missing run identity instead of opening undefined SSE URLs", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ status: "running" }), { status: 200 }),
        ),
    );
    await expect(api.run("run-real")).rejects.toThrow("运行身份缺失");
  });
  it("sends idempotency identity with the exact original request body", async () => {
    const body = {
      prompt: "检查文件",
      generation: { temperature: 0 },
      attachment_ids: [],
      mode: "auto",
      execution_target: "source" as const,
      model: null,
    };
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: "run-real",
          session_id: "session-real",
          status: "accepted",
        }),
        { status: 202 },
      ),
    );
    vi.stubGlobal("fetch", fetch);
    await api.submit("session-real", body, "stable-key");
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify(body),
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "stable-key",
      },
    });
  });
});

describe("durable Session creation", () => {
  it("reuses the request identity after a lost response, then starts a new intent", async () => {
    sessionStorage.clear();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new Error("connection lost"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ session_id: "one" }), { status: 201 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ session_id: "two" }), { status: 201 }),
      );
    vi.stubGlobal("fetch", fetch);
    await expect(api.createSession("workspace")).rejects.toThrow(
      "connection lost",
    );
    expect(await api.createSession("workspace")).toEqual({ session_id: "one" });
    expect(await api.createSession("workspace")).toEqual({ session_id: "two" });
    const keys = fetch.mock.calls.map(
      (call) => call[1].headers["Idempotency-Key"],
    );
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[1]);
  });
});
