import { request } from "./client";
export interface SummaryVersion {
  id: number;
  covered_seq: number;
  source_digest: string;
  policy_version: number;
  provider: string;
  model: string;
  summary_characters: number;
}
export interface SummaryEntry {
  text: string;
  status?: string;
  src?: { msg: number; quote: string }[];
}
export interface SummaryDetail extends SummaryVersion {
  summary_hash: string;
  summary: string;
  summary_truncated: boolean;
  structured: ({ goal: string } & Record<string, unknown>) | null;
  source_verified: boolean;
  source_problem: string | null;
  input_verification: string;
  history_hash: string | null;
  history_message_count: number | null;
  citations_problem: string | null;
}
export interface SummaryPage {
  session_id: string;
  versions: SummaryVersion[];
  next_before: number | null;
}
export interface SummaryEvidence {
  checkpoint_id: number;
  source_digest: string;
  history_hash: string;
  input_verification: string;
  message_index: number;
  offset: number;
  content: string;
  total_characters: number;
  next_offset: number | null;
}
const path = (session: string) =>
  `/api/sessions/${encodeURIComponent(session)}/context-summaries`;
export const contextSummariesApi = {
  list: (session: string, before?: number) =>
    request<SummaryPage>(
      `${path(session)}${before === undefined ? "" : `?before=${before}`}`,
    ),
  detail: (session: string, id: number) =>
    request<SummaryDetail>(`${path(session)}/${id}`),
  message: (session: string, id: number, index: number, offset = 0) =>
    request<SummaryEvidence>(
      `${path(session)}/${id}/messages/${index}?offset=${offset}`,
    ),
};
