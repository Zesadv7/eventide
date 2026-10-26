import { request } from "./client";
export interface InputManifest {
  seq: number;
  attempt_id: string;
  request_id: string;
  kind: string;
  model: string;
  message_count: number;
  tool_count: number;
  system_hash: string;
  messages_hash: string;
  tools_hash: string;
  tool_names: string[];
  tool_result_ids: string[];
  composition: { total_characters: number; components: Record<string, number> };
  instructions: null | {
    source: string;
    scope: string;
    loaded_characters: number;
    truncated: boolean;
    file_bytes: number | null;
    limit: number;
  };
}
export interface InputManifests {
  run_id: string;
  entries: InputManifest[];
  next_before: number | null;
  recorded_count: number;
  high_water: number;
}
export const inputManifestsApi = {
  list: (run: string, before?: number) =>
    request<InputManifests>(
      `/api/runs/${encodeURIComponent(run)}/input-manifests${before === undefined ? "" : `?before=${before}`}`,
    ),
};
