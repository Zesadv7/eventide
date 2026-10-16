import { ApiError, request } from "./client";
import { durableMutation } from "./mutations";
import type { components } from "./schema";
export type ImportDetail = components["schemas"]["HistoryImportDetail"];
export type ImportReceipt = components["schemas"]["HistoryImportReceipt"];
export type ImportIntent =
  | { action: "preview"; body: components["schemas"]["HistoryPreviewBody"] }
  | { action: "import"; body: components["schemas"]["HistoryImportBody"] };
const prefix = "eventide.history-import.v1.";
const active = new Set<string>(),
  listeners = new Set<() => void>();
export function importState(project: string) {
  let pending: ImportIntent | null = null;
  try {
    const item = JSON.parse(sessionStorage.getItem(prefix + project) || "null");
    if (item?.action === "preview" && typeof item.body?.source === "string")
      pending = item;
    if (
      item?.action === "import" &&
      typeof item.body?.preview_id === "string" &&
      /^[a-f0-9]{64}$/.test(item.body?.archive_sha256) &&
      item.body?.confirmed === true
    )
      pending = item;
  } catch {
    /* Ignore invalid local browser data. */
  }
  return { active: active.has(project), pending };
}
export function observeImports(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
function changed() {
  for (const listener of listeners) listener();
}
export const importsApi = {
  list: (project: string, before?: string) =>
    request<ImportDetail[]>(
      "/api/projects/" +
        encodeURIComponent(project) +
        "/history-imports?limit=20" +
        (before ? "&before=" + encodeURIComponent(before) : ""),
    ),
  detail: (id: string) =>
    request<ImportDetail>("/api/history-imports/" + encodeURIComponent(id)),
  cancel: (id: string) =>
    durableMutation<ImportDetail>(
      "history-import-cancel",
      "/api/history-imports/" + encodeURIComponent(id) + "/cancel",
      "POST",
      {},
    ),
  async submit(project: string, intent: ImportIntent): Promise<ImportReceipt> {
    if (active.has(project)) throw new Error("导入请求正在接收。");
    const pending = importState(project).pending;
    if (pending && JSON.stringify(pending) !== JSON.stringify(intent))
      throw new Error("上次请求尚未确认，请重试原请求。");
    sessionStorage.setItem(prefix + project, JSON.stringify(intent));
    active.add(project);
    changed();
    try {
      const result = await durableMutation<ImportReceipt>(
        "history-import",
        "/api/projects/" +
          encodeURIComponent(project) +
          "/history-imports" +
          (intent.action === "preview" ? "/preview" : ""),
        "POST",
        intent.body,
      );
      sessionStorage.removeItem(prefix + project);
      return result;
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
