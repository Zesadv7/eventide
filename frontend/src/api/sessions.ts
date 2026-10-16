import { ApiError, request } from "./client";
import { durableMutation } from "./mutations";
import type { components } from "./schema";
import type { Session } from "./types";
export type SessionIntent =
  | {
      session: string;
      action: "update";
      body: components["schemas"]["SessionUpdateBody"];
    }
  | {
      session: string;
      action: "delete";
      body: components["schemas"]["SessionDeleteBody"];
    };
const prefix = "eventide.session-management.v1.";
const active = new Set<string>();
const listeners = new Set<() => void>();
export function sessionMutationState(project: string) {
  let pending: SessionIntent | null = null;
  try {
    const value = JSON.parse(
      sessionStorage.getItem(prefix + project) || "null",
    );
    if (
      value &&
      ["update", "delete"].includes(value.action) &&
      typeof value.session === "string" &&
      /^[a-f0-9]{64}$/.test(value.body?.expected_metadata_hash)
    )
      pending = value;
  } catch {
    /* Keep malformed browser data out of requests. */
  }
  return { pending, active: active.has(project) };
}
function changed() {
  for (const listener of listeners) listener();
}
export function observeSessionMutations(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
export const sessionsApi = {
  list: (project: string) =>
    request<Session[]>(
      `/api/projects/${encodeURIComponent(project)}/sessions?include_archived=true`,
    ),
  async mutate(project: string, intent: SessionIntent): Promise<unknown> {
    if (active.has(project)) throw new Error("会话管理请求正在接收。");
    const pending = sessionMutationState(project).pending;
    if (pending && JSON.stringify(pending) !== JSON.stringify(intent))
      throw new Error("上次请求尚未确认，请重试原会话管理请求。");
    sessionStorage.setItem(prefix + project, JSON.stringify(intent));
    active.add(project);
    changed();
    try {
      const value = await durableMutation(
        "session-management",
        `/api/sessions/${encodeURIComponent(intent.session)}`,
        intent.action === "delete" ? "DELETE" : "PATCH",
        intent.body,
      );
      sessionStorage.removeItem(prefix + project);
      return value;
    } catch (e) {
      if (
        e instanceof ApiError &&
        ([403, 404, 422].includes(e.status) || e.mutationOutcome === "rejected")
      )
        sessionStorage.removeItem(prefix + project);
      throw e;
    } finally {
      active.delete(project);
      changed();
    }
  },
};
