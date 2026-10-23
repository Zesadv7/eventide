import { request } from "./client";
import { durableMutation } from "./mutations";

export interface FileOperation {
  operation_id: string;
  session_id: string;
  run_id: string;
  call_id: string;
  workspace_id: string;
  path: string;
  tool: string;
  status:
    | "prepared"
    | "completed"
    | "no_change"
    | "failed"
    | "needs_review"
    | "undo_prepared"
    | "undone";
  change_kind: "added" | "modified" | "unchanged";
  before: Record<string, unknown>;
  after: Record<string, unknown> | null;
}
export interface FileOperationDetail extends FileOperation {
  patch: string | null;
  diff_status: "available" | "too_large" | "unavailable";
  diff_message: string | null;
}
const path = (session: string, operation?: string) =>
  `/api/sessions/${encodeURIComponent(session)}/file-operations${operation ? `/${encodeURIComponent(operation)}` : ""}`;
export const fileOperationsApi = {
  list: (session: string) =>
    request<{ operations: FileOperation[] }>(path(session)),
  detail: (session: string, operation: string) =>
    request<FileOperationDetail>(path(session, operation)),
  undo: (session: string, operation: string) =>
    durableMutation<FileOperation>(
      "file-operation-undo",
      `${path(session, operation)}/undo`,
      "POST",
      undefined,
    ),
};
