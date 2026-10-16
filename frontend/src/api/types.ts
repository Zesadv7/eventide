import type { components } from "./schema";
export type Generation = components["schemas"]["GenerationPatch"];
export type RunInput = components["schemas"]["RunBody"];
export type ProviderInput = components["schemas"]["ProviderConfigBody"];
export interface Workspace {
  workspace_id: string;
  project_id?: string;
  source_workspace_id?: string;
  kind?: "source" | "worktree";
  name: string;
  path: string;
  git_branch?: string | null;
  git_head?: string | null;
}
export interface Run {
  run_id: string;
  session_id?: string;
  status: string;
  output?: string;
  reason?: string | null;
  error?: string | null;
  pending_approvals?: Record<string, Approval>;
  tools?: Record<string, unknown>;
  steps?: number;
  tool_calls?: number;
  started_at?: number;
  usage?: Record<string, number>;
  execution?: Execution;
}
export interface Execution {
  kind: "source" | "snapshot" | "worktree" | "evaluation";
  evaluation_id?: string;
  execution_workspace_id?: string;
  name?: string;
  base_commit?: string;
  workspace_id?: string;
  snapshot_id?: string;
  working_directory?: string;
  skipped?: number;
}
export interface Approval {
  approval_id: string;
  tool?: string;
  tool_name?: string;
  reason?: string;
  arguments?: unknown;
  [key: string]: unknown;
}
export interface Session {
  session_id: string;
  workspace_id: string;
  title: string;
  status: string;
  working_directory: string;
  archived: boolean;
  latest_run: Run | null;
  execution?: Execution;
  task_plan?: Array<{
    id: string;
    content: string;
    status: string;
    summary?: string;
  }>;
}
export interface Message {
  role: string;
  content:
    | string
    | Array<{
        type: string;
        text?: string;
        content?: string;
        name?: string;
        id?: string;
        [key: string]: unknown;
      }>;
}
export interface ProviderStatus {
  config_hash: string;
  provider: ProviderInput["provider"];
  model: string;
  base_url: string | null;
  api_key_configured: boolean;
  api_key_status: string;
  api_key_source: string;
  persistence: string;
  configuration_error?: string | null;
}
export interface GenerationState {
  config_hash: string;
  model: string;
  model_identity: string;
  defaults: Generation;
  session: Generation;
  capabilities: {
    source: string;
    temperature_max: number | null;
    top_p: boolean;
    reasoning_efforts: string[];
    thinking: boolean;
  };
}
export interface InputComposition {
  method: "serialized_unicode_characters_v1";
  unit: "characters";
  total_characters: number;
  components: {
    instructions: number;
    history: number;
    tool_catalog: number;
    skills: number;
    retrieval: number;
    tool_results: number;
    envelope: number;
  };
}
export interface UsageCall {
  run_id: string;
  request_id: string | null;
  attempt_id: string;
  kind: string;
  model?: string | null;
  provider?: string | null;
  duration_ms?: number | null;
  stop_reason?: string | null;
  input_composition?: InputComposition | null;
  status: string;
  started_at: number;
  finished_at: number | null;
  usage: Record<string, number>;
  usage_source: "reported" | "estimated" | "unknown";
  usage_complete: boolean;
}
export interface UsageLedger {
  calls: UsageCall[];
  reported_totals: Record<string, number>;
  estimated_totals: Record<string, number>;
  attempt_count: number;
  unknown_attempts: number;
  incomplete_attempts: number;
  complete: boolean;
}
export interface Capabilities {
  skills: Array<{ name: string; description: string }>;
  mcp: Array<{ name: string; transport: string }>;
  notes: string[];
  paths?: Record<string, string>;
}
export interface Attachment {
  attachment_id: string;
  name: string;
  size: number;
  media_type: string;
  kind: string;
  used: boolean;
}
export interface Receipt {
  run_id: string;
  session_id: string;
  status: string;
}
