import { request } from "./client";

export interface RecoveryAction {
  available: boolean;
  method: string;
  path: string;
  reason: string;
}
export interface ModelRetryWait {
  attempt?: number;
  category?: string;
  delay_seconds?: number;
  retry_at: number;
  [key: string]: unknown;
}
export interface UnknownTool {
  call_id: string | null;
  name: string | null;
  error: string | null;
  at: number | null;
}
export interface RecoveryClaim {
  resumed_as: string | null;
  verdict: string | null;
  at: number | null;
}
export interface RecoveryBlocked {
  reason: string | null;
  resumed_as: string | null;
  at: number | null;
}
export interface SuccessorCurrent {
  id?: string;
  run_id?: string;
  status?: string;
  steps?: number;
  [key: string]: unknown;
}
export interface RecoveryView {
  projection_version: number;
  run_id: string;
  session_id: string;
  status: string;
  is_terminal: boolean;
  high_water: number;
  continuation_of: string | null;
  summary: {
    steps: number;
    tool_calls: number;
    checkpoint_seq: number | null;
    usage: Record<string, number>;
  };
  model_retry: ModelRetryWait | null;
  stop: { kind: string; reason: string | null; error: string | null };
  unknown_tools: UnknownTool[];
  pending_approvals: { approval_id: string; summary: unknown }[];
  recovery: {
    frozen_policy: Record<string, unknown> | null;
    history_available: boolean;
    recovery_of: string | null;
    claims: RecoveryClaim[];
    successor_run_id: string | null;
    blocked: RecoveryBlocked[];
    successor: {
      run_id: string;
      admitted: boolean;
      current: SuccessorCurrent | null;
    } | null;
  };
  available_actions: { stop: RecoveryAction; continue: RecoveryAction };
  notes: string[];
}

const id = encodeURIComponent;
export const recoveryApi = {
  detail: (run: string) =>
    request<RecoveryView>(`/api/runs/${id(run)}/recovery`),
};
