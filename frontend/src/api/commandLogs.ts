import { request } from "./client";

export interface CommandLogStreamFacts {
  received_bytes: number | null;
  stored_bytes: number;
  stored_chars: number;
  sha256: string | null;
  sealed: boolean;
  encoding_loss: boolean;
  capture_state: string;
  reason: string | null;
}

export interface CommandLogReference {
  artifact_id?: string;
  run_id: string;
  call_id: string;
  streams: Record<string, CommandLogStreamFacts>;
  available?: boolean;
  availability?: string;
  detail?: string;
}

export interface CommandLogPage extends CommandLogStreamFacts {
  available: boolean;
  availability?: string;
  stream?: string;
  start?: number;
  end?: number;
  total_chars?: number;
  next_offset?: number | null;
  text: string;
}

export const commandLogsApi = {
  page: (
    runId: string,
    callId: string,
    stream: "stdout" | "stderr",
    offset: number,
    limit: number,
  ) =>
    request<CommandLogPage>(
      `/api/runs/${encodeURIComponent(runId)}/calls/${encodeURIComponent(callId)}/command-log?stream=${stream}&offset=${offset}&limit=${limit}`,
    ),
};
