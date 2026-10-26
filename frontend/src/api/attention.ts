import { request } from "./client";

export interface AttentionToolSummary {
  tool: string | null;
  command: string | null;
  targets: string[];
  query: string | null;
  cwd: string | null;
  execution_kind: string | null;
  execution_label: string | null;
  worktree_name: string | null;
  sandbox_id: string | null;
  impact: string | null;
  mcp_server: string | null;
  mcp_tool: string | null;
  network_access: null;
  privilege_elevation: null;
}

export interface AttentionApproval {
  kind: "approval";
  dedup_key: string;
  approval_id: string;
  run_id: string;
  session_id: string;
  workspace_id: string;
  ts: number;
  tool: unknown;
  arguments: unknown;
  reason: string | null;
  summary: AttentionToolSummary;
  run_status: string;
  actionable: boolean;
  action: { method: string; url: string };
  links: { run: string; session: string };
}

export interface AttentionRun {
  kind: "run";
  dedup_key: string;
  run_id: string;
  session_id: string;
  workspace_id: string;
  ts: number | null;
  status: string;
  error: string | null;
  reason: string | null;
  pending_approvals: number;
  worker_role?: unknown;
  parent_run_id?: unknown;
  links: { run: string; session: string };
}

export interface AttentionDelivery {
  kind: "delivery_candidate";
  dedup_key: string;
  delivery_id: string;
  name: string | null;
  workspace_id: string | null;
  project_id: string;
  patch_sha256: string | null;
  links: { page: string };
}

export interface AttentionSnapshot {
  generated_at: number;
  project_id: string | null;
  limits?: { approvals: number; runs: number };
  counts: { approvals: number; runs: number; delivery_candidates: number };
  approvals: AttentionApproval[];
  runs: AttentionRun[];
  delivery_candidates: AttentionDelivery[];
  unsupported: Record<string, string>;
}

export const attentionApi = {
  project: (project: string, approvalsLimit = 50, runsLimit = 20) =>
    request<AttentionSnapshot>(
      `/api/projects/${encodeURIComponent(project)}/attention?approvals_limit=${approvalsLimit}&runs_limit=${runsLimit}`,
    ),
  global: (approvalsLimit = 50, runsLimit = 20) =>
    request<AttentionSnapshot>(
      `/api/attention?approvals_limit=${approvalsLimit}&runs_limit=${runsLimit}`,
    ),
};
