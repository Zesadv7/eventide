import {request} from "./transport.js?v=13";

const storageKey = "eventide.pending-connection-check.v1";
let busy = false;
export const hasPendingConnection = () => sessionStorage.getItem(storageKey) !== null;

// Persist only a random request identity and a one-way form fingerprint. The
// password and candidate configuration stay in memory and in the request body.
export async function testConnection(body) {
  if (busy) throw new Error("连接检查正在进行，请等待当前结果");
  busy = true;
  try {
    const bytes = new TextEncoder().encode(JSON.stringify({
      provider: body.provider, model: body.model, base_url: body.base_url ?? null,
      api_key: body.api_key ?? null,
    }));
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    const fingerprint = Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, "0")).join("");
    const saved = sessionStorage.getItem(storageKey);
    let pending;
    if (saved) {
      try { pending = JSON.parse(saved); } catch { throw new Error("待确认连接检查记录损坏，请保留当前标签页并检查服务端记录"); }
      if (!pending?.key || !pending?.fingerprint) throw new Error("待确认连接检查记录不完整，请检查服务端记录");
      if (pending.fingerprint !== fingerprint) throw new Error("上次连接检查结果尚未确认，请恢复原候选配置后重试；不会重新收费派发");
    } else {
      pending = {key: crypto.randomUUID(), fingerprint};
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
    }
    try {
      const result = await request("/api/config/provider/test", {
        method: "POST", headers: {"Content-Type": "application/json", "Idempotency-Key": pending.key},
        body: JSON.stringify(body),
      });
      sessionStorage.removeItem(storageKey);
      return result;
    } catch (error) {
      // These statuses reject before admission. A network failure, 5xx or 409
      // proves nothing about dispatch, so the original key must survive reload.
      if ([403, 404, 422].includes(error.status)) sessionStorage.removeItem(storageKey);
      throw error;
    }
  } finally { busy = false; }
}
