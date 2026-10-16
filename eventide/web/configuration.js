import {request} from "./transport.js?v=14";

const storageKey = "eventide.legacy-configuration.v1";
let active = false;
export function configurationStatus() {
  const raw = sessionStorage.getItem(storageKey);
  let operation;
  try { operation = raw ? JSON.parse(raw).operation : undefined; } catch { /* Keep corrupt requests blocked. */ }
  return {active, pending: raw !== null, operation};
}
export function rejectedConfiguration(error) {
  return [403, 404, 422].includes(error.status) || error.mutationOutcome === "rejected";
}
export async function mutateConfiguration(operation, candidate, expected) {
  if (active) throw new Error("配置请求正在进行，请等待当前结果");
  active = true;
  try {
    const method = operation === "reset" ? "DELETE" : "PUT";
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(candidate)));
    const fingerprint = Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, "0")).join("");
    const raw = sessionStorage.getItem(storageKey);
    let pending;
    if (raw !== null) {
      try { pending = JSON.parse(raw); } catch { throw new Error("待确认配置请求记录损坏，请核对服务端记录"); }
      if (!pending?.key || !/^[a-f0-9]{64}$/.test(pending.expected_config_hash) || !pending.fingerprint)
        throw new Error("待确认配置请求记录不完整，请核对服务端记录");
      if (pending.operation !== operation || pending.fingerprint !== fingerprint)
        throw new Error("上次配置请求尚未确认，请恢复原候选参数和密钥后重试同一操作");
    } else {
      if (!/^[a-f0-9]{64}$/.test(expected)) throw new Error("缺少配置版本，请先刷新当前配置");
      pending = {operation, expected_config_hash: expected, fingerprint, key: crypto.randomUUID()};
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
    }
    try {
      const result = await request("/api/config/provider", {
        method, headers: {"Content-Type": "application/json", "Idempotency-Key": pending.key},
        body: JSON.stringify({...candidate, expected_config_hash: pending.expected_config_hash}),
      });
      sessionStorage.removeItem(storageKey);
      return result;
    } catch (error) {
      if (rejectedConfiguration(error)) sessionStorage.removeItem(storageKey);
      throw error;
    }
  } finally { active = false; }
}
