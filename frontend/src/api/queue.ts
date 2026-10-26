import { request } from "./client";
import type { paths } from "./schema";
import type { Execution } from "./types";

export interface QueueEntry {
  run_id: string;
  session_id: string;
  project_id: string | null;
  workspace_id: string;
  execution: Execution;
  admitted_at: number;
  position: number | null;
  status: string;
  dispatch_state: "queued" | "starting" | "running" | "finishing";
}
export interface QueueSnapshot {
  limits: {
    active_roots: number;
    model_requests: number;
    waiting_roots: number;
    readonly_tools?: number;
  };
  counts: {
    active_roots: number;
    waiting_roots: number;
    preparing_admissions: number;
    active_models: number;
    waiting_models: number;
    active_readonly_tools?: number;
    waiting_readonly_tools?: number;
  };
  queued: QueueEntry[];
  active: QueueEntry[];
}
const queuePath: keyof paths = "/api/queue";
export const queueApi = { status: () => request<QueueSnapshot>(queuePath) };
