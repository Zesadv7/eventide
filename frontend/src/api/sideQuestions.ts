import { request } from "./client";
import { durableMutation } from "./mutations";

export type SideRunStatus =
  "queued" | "running" | "completed" | "failed" | "interrupted" | "unknown";

export interface SideQuestionRecord {
  side_question_id: string;
  side_run_id: string;
  side_session_id: string;
  source_run_id: string;
  source_session_id: string;
  question: string;
  source_high_water: number;
  snapshot_id: string;
  files_captured_at?: string;
  source_execution?: { kind: string };
  source_context?: {
    user_prompt?: string;
    latest_output?: string;
    tool_activity?: string;
    evidence_events?: number;
  };
  status: string;
  turn_id?: string;
}

export interface SideQuestionView extends SideQuestionRecord {
  side_run_status: SideRunStatus;
  answer: string;
  cleanup_error?: string | null;
  answer_fingerprint: string | null;
  returned: boolean;
  supplement: {
    supplement_id?: string;
    status?: string;
    consumed?: boolean;
  } | null;
}

export interface SideQuestionProjection {
  run_id: string;
  side_questions: SideQuestionView[];
}

export interface SideQuestionCreated extends SideQuestionRecord {
  snapshot_id: string;
}

export interface SideQuestionReturnReceipt {
  side_question_id: string;
  side_run_id: string;
  source_run_id: string;
  supplement_id?: string;
  status: string;
  returned: boolean;
  already_returned: boolean;
  answer_fingerprint: string;
  body_truncated: boolean;
  answer_length?: number | null;
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text),
  );
  return Array.from(new Uint8Array(digest), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}

export const sideQuestionsApi = {
  fingerprint: sha256Hex,
  async list(runId: string): Promise<SideQuestionProjection> {
    return request<SideQuestionProjection>(
      `/api/runs/${encodeURIComponent(runId)}/side-questions`,
    );
  },
  async get(sideQuestionId: string): Promise<SideQuestionView> {
    return request<SideQuestionView>(
      `/api/side-questions/${encodeURIComponent(sideQuestionId)}`,
    );
  },
  async ask(
    sessionId: string,
    question: string,
    sourceRunId: string,
  ): Promise<SideQuestionCreated> {
    return durableMutation<SideQuestionCreated>(
      "side_question_create",
      `/api/sessions/${encodeURIComponent(sessionId)}/side-questions`,
      "POST",
      { question, source_run_id: sourceRunId },
    );
  },
  async return(
    sideQuestionId: string,
    answer: string,
  ): Promise<SideQuestionReturnReceipt> {
    return durableMutation<SideQuestionReturnReceipt>(
      "side_question_return",
      `/api/side-questions/${encodeURIComponent(sideQuestionId)}/return`,
      "POST",
      { answer_fingerprint: await sha256Hex(answer) },
    );
  },
};
