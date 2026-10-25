import { request } from "./client";

export interface DiagnosticReference {
  event_type: string | null;
  at: number | null;
  call_id: string | null;
  attempt_id: string | null;
}
export interface KnownCause {
  code: string;
  label: string;
  detail: string | null;
  ref: DiagnosticReference | null;
}
export interface PendingItem {
  code: string;
  label: string;
  detail: string | null;
  ref: DiagnosticReference | null;
}
export interface ToolFact {
  call_id: string;
  name: string;
  readonly: boolean;
  status: string;
  detail: string | null;
  duration_ms: number | null;
  at: number | null;
}
export interface AttemptFact {
  attempt_id: string;
  attempt: number | null;
  kind: string | null;
  status: string;
  usage_source: string;
  model: string | null;
  started_at: number | null;
  finished_at: number | null;
}
export interface RunSummary {
  what_happened: string;
  where_stopped: string | null;
  completed: string;
  unknowns: string[];
  next_steps: string[];
}
export interface RunDiagnosticsReport {
  report_version: number;
  source: string;
  run_id: string;
  high_water: number;
  facts_available: boolean;
  status: string;
  stop_reason: string | null;
  mode: string | null;
  provider: string | null;
  model: string | null;
  started_at: number | null;
  ended_at: number | null;
  counts: Record<string, number>;
  summary: RunSummary;
  known_causes: KnownCause[];
  pending: PendingItem[];
  tools: ToolFact[];
  attempts: AttemptFact[];
  unavailable: string[];
  notes: string[];
  truncated: boolean;
}

const id = encodeURIComponent;
export type DiagnosticsFormat = "json" | "markdown";
export const diagnosticsApi = {
  report: (run: string, highWater?: number) =>
    request<RunDiagnosticsReport>(
      `/api/runs/${id(run)}/diagnostics${
        highWater === undefined ? "" : `?high_water=${highWater}`
      }`,
    ),
  // Downloads are bound to the exact high-water mark of the displayed report;
  // later events can never leak into an already generated version.
  downloadUrl: (run: string, format: DiagnosticsFormat, highWater: number) =>
    `/api/runs/${id(run)}/diagnostics/download?format=${format}&high_water=${highWater}`,
};
