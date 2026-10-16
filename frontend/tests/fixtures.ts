import type {
  Session,
  Run,
  ProviderStatus,
  GenerationState,
  UsageLedger,
} from "../src/api/types";
import type { BudgetStatus, Costs } from "../src/api/budgets";
export const provider: ProviderStatus = {
  config_hash: "a".repeat(64),
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
  config_hash: "b".repeat(64),
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
export const costs: Costs = {
  known_costs: {},
  estimated_usage_costs: {},
  unknown_attempts: 1,
  estimated_attempts: 0,
  unpriced_attempt_ids: ["attempt-1"],
  historical_unreserved_attempt_ids: [],
  complete: false,
  latest: [],
  history: [],
  usage: ledger,
  attempt_count: 1,
  timezone: "UTC",
};
export const budgetStatus: BudgetStatus = {
  timezone: "UTC",
  budgets: [
    {
      kind: "project",
      identity: "project-1",
      token_limit: null,
      actual_tokens: 0,
      reserved_tokens: 42,
      committed_tokens: 42,
      unsettled_attempts: 1,
      remaining_tokens: null,
      warning: false,
      blocked: false,
      configuration_hash: "a".repeat(64),
    },
  ],
  reservations: [
    {
      attempt_id: "attempt-1",
      run_id: "run-1",
      session_id: "session-1",
      status: "unsettled",
      reserved_tokens: 42,
      actual_tokens: null,
      usage_source: "unknown",
      input_estimate: 32,
      output_estimate: 10,
      estimate_method: "serialized_utf8_bytes_v1",
      reserved_at: "2026-10-16T00:00:00+00:00",
      context: {
        project_id: "project-1",
        root_run_id: "run-1",
        delivery_id: null,
      },
      audit: [],
    },
  ],
};
