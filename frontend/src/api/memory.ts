import { ApiError, request } from "./client";
import { durableMutation } from "./mutations";

export type MemoryScope = "global" | "project";
export interface MemorySource {
  type: "manual" | "explicit" | "suggestion";
  event_ids: string[];
  session_id: string | null;
  span?: { start: number; end: number };
}
export interface MemoryRecord {
  id: string;
  revision: number;
  scope: MemoryScope;
  project_id: string | null;
  key: string;
  content: string;
  status: "active" | "paused" | "revoked";
  source: MemorySource;
  created_at: number;
  updated_at: number;
}
export interface MemorySuggestion extends Omit<MemoryRecord, "status"> {
  status: "pending" | "accepted" | "dismissed";
}
export interface MemoryState {
  memories: MemoryRecord[];
  suggestions: MemorySuggestion[];
  settings: { enabled: boolean; revision: number };
  warning: string;
}
export interface MemoryReference {
  id: string;
  revision: number;
  scope: MemoryScope;
  project_id: string | null;
  key: string;
}
export interface RunMemory {
  snapshotted: boolean;
  snapshot?: { memories: MemoryReference[] } | null;
  injections: {
    request_id: string;
    attempt_id: string;
    memories: MemoryReference[];
    digest: string;
  }[];
  receipts: {
    type: string;
    memory_id?: string;
    suggestion_id?: string;
    reason?: string;
    revision?: number;
    key?: string;
    content?: string;
    scope?: MemoryScope;
    scope_defaulted?: boolean;
    current_status?: string;
    current_revision?: number;
  }[];
}
export interface MemoryCreate {
  scope: MemoryScope;
  project_id: string | null;
  key: string;
  content: string;
}
export interface MemoryResult {
  memory?: MemoryRecord;
  suggestion?: MemorySuggestion;
  settings?: MemoryState["settings"];
}
interface Intent {
  path: string;
  method: string;
  body: unknown;
}
interface Pending {
  path: string;
  method: string;
  fingerprint: string;
  template: Record<string, unknown>;
  needsKey: boolean;
  needsContent: boolean;
}
const storage = "eventide.memory-pending.v1";
let original: Intent | null = null;
let active = false;
export function memoryPending(): Pending | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storage) || "null");
    return value &&
      typeof value.path === "string" &&
      typeof value.method === "string" &&
      value.template &&
      typeof value.template === "object" &&
      typeof value.needsKey === "boolean" &&
      typeof value.needsContent === "boolean" &&
      /^[a-f0-9]{64}$/.test(value.fingerprint)
      ? value
      : null;
  } catch {
    return null;
  }
}
export const canRetryMemory = () => !!original && !!memoryPending();
async function mutate(intent: Intent): Promise<MemoryResult> {
  if (active) throw new Error("记忆请求正在接收，请等待结果。");
  active = true;
  try {
    const bytes = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(JSON.stringify(intent)),
    );
    const fingerprint = Array.from(new Uint8Array(bytes), (b) =>
      b.toString(16).padStart(2, "0"),
    ).join("");
    const pending = memoryPending();
    if (pending && pending.fingerprint !== fingerprint)
      throw new Error(
        "上次记忆操作结果未知。请先重试原操作；刷新后需重新填写完全相同的内容和版本，不能另发修改。",
      );
    // Persist only identity metadata, never the preference text or draft.
    const body = intent.body as Record<string, unknown>;
    const template = Object.fromEntries(
      Object.entries(body).map(([key, value]) => [
        key,
        key === "content" || key === "key" ? null : value,
      ]),
    );
    sessionStorage.setItem(
      storage,
      JSON.stringify({
        path: intent.path,
        method: intent.method,
        fingerprint,
        template,
        needsKey: "key" in body,
        needsContent: "content" in body,
      }),
    );
    original = intent;
    try {
      const result = await durableMutation<MemoryResult>(
        "memory",
        intent.path,
        intent.method,
        intent.body,
      );
      sessionStorage.removeItem(storage);
      original = null;
      return result;
    } catch (error) {
      if (
        error instanceof ApiError &&
        ([403, 404, 422].includes(error.status) ||
          error.mutationOutcome === "rejected")
      ) {
        sessionStorage.removeItem(storage);
        original = null;
      }
      throw error;
    }
  } finally {
    active = false;
  }
}
export const memoryApi = {
  list: (project?: string) =>
    request<MemoryState>(
      `/api/memory?include_revoked=true${project ? `&project_id=${encodeURIComponent(project)}` : ""}`,
    ),
  run: (run: string) =>
    request<RunMemory>(`/api/runs/${encodeURIComponent(run)}/memory`),
  create: (body: MemoryCreate) =>
    mutate({ path: "/api/memory", method: "POST", body }),
  update: (
    id: string,
    expected_revision: number,
    changes: { content?: string; status?: MemoryRecord["status"] },
  ) =>
    mutate({
      path: `/api/memory/${encodeURIComponent(id)}`,
      method: "PATCH",
      body: { expected_revision, ...changes },
    }),
  revoke: (id: string, expected_revision: number) =>
    mutate({
      path: `/api/memory/${encodeURIComponent(id)}`,
      method: "DELETE",
      body: { expected_revision },
    }),
  settings: (enabled: boolean, expected_revision: number) =>
    mutate({
      path: "/api/memory/settings",
      method: "PATCH",
      body: { expected_revision, enabled },
    }),
  resolve: (
    id: string,
    expected_revision: number,
    action: "accept" | "dismiss",
  ) =>
    mutate({
      path: `/api/memory/suggestions/${encodeURIComponent(id)}/resolve`,
      method: "POST",
      body: { expected_revision, action },
    }),
  recover: (content: string, key: string) => {
    const pending = memoryPending();
    if (!pending) return Promise.reject(new Error("没有待确认的记忆请求。"));
    const body = { ...pending.template };
    if (pending.needsKey) body.key = key;
    if (pending.needsContent) body.content = content;
    return mutate({ path: pending.path, method: pending.method, body });
  },
  retry: () =>
    original
      ? mutate(original)
      : Promise.reject(
          new Error("原内容未保存在浏览器，请重新填写完全相同的原操作。"),
        ),
};
