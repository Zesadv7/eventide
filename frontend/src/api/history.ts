import { api, request } from "./client";
import type { paths } from "./schema";
import type { Execution, Run, Session } from "./types";

export type HistorySession = Omit<Session, "execution"> & {
  project_id: string;
  updated_at?: number;
};
export type HistoryRun = Omit<Run, "execution"> & {
  execution?:
    | Execution
    | { kind: "evaluation"; workspace_id: string; evaluation_id: string };
  completed_at?: number;
  duration_ms?: number;
  continuation_of?: string | null;
  parent_run_id?: string;
  gap_files?: unknown;
};
const sessionPath: keyof paths = "/api/projects/{project_id}/sessions";

export const historyApi = {
  sessions: (project: string) =>
    request<HistorySession[]>(
      `${sessionPath.replace("{project_id}", encodeURIComponent(project))}?include_archived=true`,
    ),
  // The backend returns each newest page in chronological order. The first
  // identity is its exclusive before cursor; timestamps are not pagination keys.
  runs: (session: string, before?: string): Promise<HistoryRun[]> =>
    api.runs(session, before),
  run: (id: string): Promise<HistoryRun> => api.run(id),
  exportUrl: (id: string) => `/api/runs/${encodeURIComponent(id)}/export`,
};
