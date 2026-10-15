import type {
  Session,
  Run,
  ProviderStatus,
  GenerationState,
  UsageLedger,
} from "../src/api/types";
export const provider: ProviderStatus = {
  provider: "openai_responses",
  model: "fixture-model",
  base_url: null,
  api_key_configured: true,
  api_key_status: "configured",
  api_key_source: "sqlite_encrypted",
  persistence: "sqlite_encrypted",
};
export const run: Run = {
  run_id: "run-1",
  session_id: "session-1",
  status: "completed",
  pending_approvals: {},
  steps: 1,
  tool_calls: 0,
};
export const session: Session = {
  session_id: "session-1",
  workspace_id: "workspace-1",
  title: "测试对话",
  status: "completed",
  working_directory: "/fixture",
  archived: false,
  latest_run: run,
};
export const generation: GenerationState = {
  model: "fixture-model",
  model_identity: "fixture",
  defaults: {},
  session: {},
  capabilities: {
    source: "declared",
    temperature_max: 2,
    top_p: true,
    reasoning_efforts: ["none", "low", "high"],
    thinking: false,
  },
};
export const ledger: UsageLedger = {
  calls: [
    {
      run_id: "run-1",
      request_id: "request-1",
      attempt_id: "attempt-1",
      kind: "agent",
      model: "fixture-model",
      status: "failed",
      started_at: 1000,
      finished_at: 1001,
      usage: {},
      usage_source: "unknown",
      usage_complete: false,
    },
  ],
  reported_totals: {},
  estimated_totals: {},
  attempt_count: 1,
  unknown_attempts: 1,
  incomplete_attempts: 1,
  complete: false,
};
