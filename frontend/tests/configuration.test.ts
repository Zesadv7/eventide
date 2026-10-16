import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  configurationMutation,
  configurationStatus,
} from "../src/api/configuration";
const hash = "a".repeat(64);
const candidate = {
  model: "private-model",
  api_key: "private-secret",
  generation: { temperature: 0, top_p: null },
};
const save = (body = candidate, version = hash) =>
  configurationMutation(
    "provider",
    "/api/config/provider",
    "PUT",
    body,
    version,
  );
beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());
it("retries an unknown response using the original identity and hash without storing secrets", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(Response.json({ ok: true }));
  vi.stubGlobal("fetch", fetch);
  await expect(save()).rejects.toThrow("offline");
  expect(configurationStatus("provider").pending).toBe(true);
  const stored = JSON.stringify({ ...sessionStorage });
  expect(stored).not.toContain(candidate.api_key);
  expect(stored).not.toContain(candidate.model);
  await expect(save({ ...candidate, model: "edited" })).rejects.toThrow(
    "原候选",
  );
  expect(fetch).toHaveBeenCalledTimes(1);
  await expect(save(candidate, "b".repeat(64))).resolves.toEqual({ ok: true });
  const first = fetch.mock.calls[0]![1],
    second = fetch.mock.calls[1]![1];
  expect(second.headers["Idempotency-Key"]).toBe(
    first.headers["Idempotency-Key"],
  );
  expect(second.body).toBe(first.body);
  expect(JSON.parse(second.body).generation).toEqual({
    temperature: 0,
    top_p: null,
  });
  expect(sessionStorage.length).toBe(0);
});
it.each([409, 503])(
  "retains an unmarked %s and clears a definitive conflict",
  async (status) => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ detail: "unknown" }, { status }))
      .mockResolvedValueOnce(
        Response.json(
          { detail: "stale" },
          {
            status: 409,
            headers: { "X-Eventide-Mutation-Outcome": "rejected" },
          },
        ),
      )
      .mockResolvedValueOnce(Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetch);
    await expect(save()).rejects.toThrow("unknown");
    expect(configurationStatus("provider").pending).toBe(true);
    await expect(save()).rejects.toThrow("stale");
    expect(configurationStatus("provider").pending).toBe(false);
    await save(candidate, "b".repeat(64));
    expect(fetch.mock.calls[2]![1].headers["Idempotency-Key"]).not.toBe(
      fetch.mock.calls[0]![1].headers["Idempotency-Key"],
    );
  },
);
it("prevents concurrent duplicate dispatch while allowing an independent scope", async () => {
  let finish!: (value: Response) => void;
  const fetch = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue(Response.json({ ok: true }));
  vi.stubGlobal("fetch", fetch);
  const first = save();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  await expect(save()).rejects.toThrow("正在进行");
  await configurationMutation(
    "session-other",
    "/api/sessions/other/generation",
    "PUT",
    { generation: { temperature: 0 } },
    hash,
  );
  finish(Response.json({ ok: true }));
  await first;
  expect(fetch).toHaveBeenCalledTimes(2);
});
