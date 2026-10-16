import { ApiError, request } from "./client";
import { durableMutation } from "./mutations";
import type { components } from "./schema";

export type WorktreeInput = components["schemas"]["WorktreeBody"];
export type WorktreeCatalog = components["schemas"]["WorktreeCatalog"];
export type WorktreeDetail = components["schemas"]["WorktreeDetail"];
export type WorktreeReceipt = components["schemas"]["WorktreeReceipt"];
const prefix = "eventide.worktree-pending.v1.";
const active = new Set<string>();
const receipts = new Map<string, WorktreeReceipt>();
const listeners = new Set<() => void>();
const keyFor = (project: string) => prefix + encodeURIComponent(project);
export function worktreeRequestState(project: string) {
  let pending: WorktreeInput | null = null;
  try {
    const value = JSON.parse(sessionStorage.getItem(keyFor(project)) || "null");
    if (
      value &&
      /^[a-f0-9]{64}$/.test(value.expected_catalog_hash) &&
      ["create", "keep", "remove"].includes(value.action) &&
      typeof value.name === "string" &&
      typeof value.ref === "string"
    )
      pending = value;
  } catch {
    /* Invalid browser state cannot become an executable request. */
  }
  return {
    pending,
    active: active.has(project),
    receipt: receipts.get(project),
  };
}
export function observeWorktrees(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
function changed() {
  for (const listener of listeners) listener();
}
export const worktreesApi = {
  catalog: (project: string) =>
    request<WorktreeCatalog>(
      `/api/projects/${encodeURIComponent(project)}/worktrees`,
    ),
  list: (project: string, before?: string) =>
    request<WorktreeDetail[]>(
      `/api/projects/${encodeURIComponent(project)}/worktree-operations?limit=20${before ? `&before=${encodeURIComponent(before)}` : ""}`,
    ),
  detail: (id: string) =>
    request<WorktreeDetail>(
      `/api/worktree-operations/${encodeURIComponent(id)}`,
    ),
  cancel: (id: string) =>
    durableMutation<WorktreeDetail>(
      "worktree-cancel",
      `/api/worktree-operations/${encodeURIComponent(id)}/cancel`,
      "POST",
      {},
    ),
  async create(
    project: string,
    input: WorktreeInput,
  ): Promise<WorktreeReceipt> {
    if (active.has(project))
      throw new Error("工作副本请求正在接收，请等待结果。");
    const body = {
      action: input.action,
      name: input.name,
      ref: input.ref ?? "HEAD",
      expected_catalog_hash: input.expected_catalog_hash,
    };
    const pending = worktreeRequestState(project).pending;
    if (pending && JSON.stringify(pending) !== JSON.stringify(body))
      throw new Error("上次接收尚未确认，请重试原工作副本请求。");
    sessionStorage.setItem(keyFor(project), JSON.stringify(body));
    active.add(project);
    changed();
    try {
      const receipt = await durableMutation<WorktreeReceipt>(
        "worktree-management",
        `/api/projects/${encodeURIComponent(project)}/worktrees`,
        "POST",
        body,
      );
      receipts.set(project, receipt);
      sessionStorage.removeItem(keyFor(project));
      return receipt;
    } catch (error) {
      if (
        error instanceof ApiError &&
        ([403, 404, 422].includes(error.status) ||
          error.mutationOutcome === "rejected")
      )
        sessionStorage.removeItem(keyFor(project));
      throw error;
    } finally {
      active.delete(project);
      changed();
    }
  },
};
