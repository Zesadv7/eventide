import { ApiError, request } from "./client";
import { durableMutation } from "./mutations";
import type { components } from "./schema";
import type { RetrievalStatus } from "./types";

export type IndexInput = components["schemas"]["IndexBuildBody"] & {
  workspace_id?: string;
};
export type IndexConfiguration = components["schemas"]["IndexConfiguration"];
export type IndexDetail = Omit<
  components["schemas"]["IndexDetail"],
  "workspace_id"
> & {
  workspace_id?: string;
};
export type IndexReceipt = Omit<
  components["schemas"]["IndexReceipt"],
  "workspace_id"
> & {
  workspace_id?: string;
};
const prefix = "eventide.index-pending.v1.";
const active = new Set<string>();
const receipts = new Map<string, IndexReceipt>();
const listeners = new Set<() => void>();
const keyFor = (project: string) => prefix + encodeURIComponent(project);
export function indexRequestState(project: string) {
  let pending: IndexInput | null = null;
  try {
    const value = JSON.parse(sessionStorage.getItem(keyFor(project)) || "null");
    if (
      value &&
      /^[a-f0-9]{64}$/.test(value.config_hash) &&
      typeof value.force === "boolean" &&
      typeof value.code_structure === "boolean" &&
      (value.workspace_id === undefined ||
        typeof value.workspace_id === "string")
    )
      pending = {
        config_hash: value.config_hash,
        force: value.force,
        code_structure: value.code_structure,
        ...(value.workspace_id === undefined
          ? {}
          : { workspace_id: value.workspace_id }),
      };
  } catch {
    /* Invalid browser state cannot become an executable request. */
  }
  return {
    pending,
    active: active.has(project),
    receipt: receipts.get(project),
  };
}
export function observeIndexes(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
function changed() {
  for (const listener of listeners) listener();
}
export const indexesApi = {
  status: (workspace: string) =>
    request<RetrievalStatus>(
      `/api/workspaces/${encodeURIComponent(workspace)}/index-status`,
    ),
  configuration: () =>
    request<IndexConfiguration>("/api/indexes/configuration"),
  list: (project: string, before?: string) =>
    request<IndexDetail[]>(
      `/api/projects/${encodeURIComponent(project)}/indexes?limit=20${before ? `&before=${encodeURIComponent(before)}` : ""}`,
    ),
  detail: (id: string) =>
    request<IndexDetail>(`/api/indexes/${encodeURIComponent(id)}`),
  cancel: (id: string) =>
    durableMutation<IndexDetail>(
      "index-cancel",
      `/api/indexes/${encodeURIComponent(id)}/cancel`,
      "POST",
      {},
    ),
  async create(project: string, input: IndexInput): Promise<IndexReceipt> {
    if (active.has(project)) throw new Error("索引请求正在接收，请等待结果。");
    const body = {
      config_hash: input.config_hash,
      force: input.force,
      code_structure: input.code_structure,
      ...(input.workspace_id === undefined
        ? {}
        : { workspace_id: input.workspace_id }),
    };
    const pending = indexRequestState(project).pending;
    if (pending && JSON.stringify(pending) !== JSON.stringify(body))
      throw new Error("上次接收尚未确认，请重试原索引请求。");
    sessionStorage.setItem(keyFor(project), JSON.stringify(body));
    active.add(project);
    changed();
    try {
      const receipt = await durableMutation<IndexReceipt>(
        "index",
        `/api/projects/${encodeURIComponent(project)}/indexes`,
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
