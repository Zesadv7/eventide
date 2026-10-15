import { ApiError } from "./client";
import { durableMutation } from "./mutations";
import type { ProviderInput, UsageLedger } from "./types";

export interface ProbeResult {
  success: boolean;
  provider: ProviderInput["provider"];
  model: string;
  run_id: string | null;
  usage: UsageLedger;
  latency_ms: number | null;
  checked_at: string | null;
  recovered: boolean;
  message: string;
  error_type: string | null;
  detail?: string;
}

const pendingKey = "eventide.probe-pending-form.v1";
let active = false;
let latestResult: ProbeResult | null = null;
const listeners = new Set<() => void>();

export function probeStatus() {
  return { active, pending: hasPendingProbe(), result: latestResult };
}

export function observeProbe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function changed() {
  for (const listener of listeners) listener();
}

export function hasPendingProbe(): boolean {
  try {
    return sessionStorage.getItem(pendingKey) !== null;
  } catch {
    return false;
  }
}

export const probeApi = {
  async test(input: ProviderInput): Promise<ProbeResult> {
    if (active) throw new Error("连接测试正在进行，请等待当前结果。");
    active = true;
    latestResult = null;
    changed();
    try {
      const body: ProviderInput = {
        provider: input.provider,
        model: input.model.trim(),
        base_url: input.base_url?.trim() || null,
        clear_api_key: false,
        ...(input.api_key?.trim() ? { api_key: input.api_key.trim() } : {}),
      };
      const hash = await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(JSON.stringify(body)),
      );
      const fingerprint = Array.from(new Uint8Array(hash), (byte) =>
        byte.toString(16).padStart(2, "0"),
      ).join("");
      const pending = sessionStorage.getItem(pendingKey);
      if (pending !== null && pending !== fingerprint)
        throw new Error(
          "上次连接测试尚未确认，请恢复原候选配置和密钥后重试同一请求。",
        );
      // Only a digest guards edits after an uncertain response. durableMutation
      // owns the random identity; neither store persists the form or password.
      sessionStorage.setItem(pendingKey, fingerprint);
      try {
        const result = await durableMutation<ProbeResult>(
          "probe",
          "/api/config/provider/test",
          "POST",
          body,
        );
        sessionStorage.removeItem(pendingKey);
        latestResult = result;
        return result;
      } catch (error) {
        if (
          error instanceof ApiError &&
          ([403, 404, 422].includes(error.status) ||
            error.mutationOutcome === "rejected")
        )
          sessionStorage.removeItem(pendingKey);
        throw error;
      }
    } finally {
      active = false;
      changed();
    }
  },
};
