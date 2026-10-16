import { ApiError, request } from "./client";

interface PendingConfiguration {
  method: string;
  path: string;
  expected_config_hash: string;
  fingerprint: string;
  key: string;
}
const active = new Set<string>();
const listeners = new Set<() => void>();
const storageKey = (scope: string) =>
  `eventide.configuration.v1.${encodeURIComponent(scope)}`;
export const generationScope = (model: string, session?: string) =>
  JSON.stringify(["generation", session || null, model]);

export function configurationStatus(scope: string) {
  const saved = sessionStorage.getItem(storageKey(scope));
  let method: string | undefined;
  try {
    method = saved
      ? (JSON.parse(saved) as PendingConfiguration).method
      : undefined;
  } catch {
    /* A corrupt pending record remains blocked. */
  }
  return { active: active.has(scope), pending: saved !== null, method };
}
export function observeConfiguration(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
function changed() {
  for (const listener of listeners) listener();
}
export function rejectedConfiguration(error: unknown) {
  return (
    error instanceof ApiError &&
    ([403, 404, 422].includes(error.status) ||
      error.mutationOutcome === "rejected")
  );
}

export async function configurationMutation<T>(
  scope: string,
  path: string,
  method: string,
  candidate: Record<string, unknown>,
  expected: string,
): Promise<T> {
  if (active.has(scope))
    throw new Error("该配置请求正在进行，请等待当前结果。");
  active.add(scope);
  changed();
  try {
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(JSON.stringify(candidate)),
    );
    const fingerprint = Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
    const saved = sessionStorage.getItem(storageKey(scope));
    let pending: PendingConfiguration;
    if (saved !== null) {
      try {
        pending = JSON.parse(saved) as PendingConfiguration;
      } catch {
        throw new Error("待确认配置请求记录损坏，请先核对服务端记录。");
      }
      if (
        !pending?.key ||
        !/^[a-f0-9]{64}$/.test(pending.expected_config_hash) ||
        !pending.fingerprint
      )
        throw new Error("待确认配置请求记录不完整，请先核对服务端记录。");
      if (
        pending.path !== path ||
        pending.method !== method ||
        pending.fingerprint !== fingerprint
      )
        throw new Error(
          "上次配置请求尚未确认，请恢复原候选参数和密钥后重试同一操作。",
        );
    } else {
      if (!/^[a-f0-9]{64}$/.test(expected))
        throw new Error("缺少配置版本，请先刷新当前配置。");
      pending = {
        method,
        path,
        expected_config_hash: expected,
        fingerprint,
        key: crypto.randomUUID(),
      };
      sessionStorage.setItem(storageKey(scope), JSON.stringify(pending));
    }
    changed();
    try {
      const result = await request<T>(path, {
        method,
        headers: { "Idempotency-Key": pending.key },
        body: JSON.stringify({
          ...candidate,
          expected_config_hash: pending.expected_config_hash,
        }),
      });
      sessionStorage.removeItem(storageKey(scope));
      return result;
    } catch (error) {
      if (rejectedConfiguration(error))
        sessionStorage.removeItem(storageKey(scope));
      throw error;
    }
  } finally {
    active.delete(scope);
    changed();
  }
}
