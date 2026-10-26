import { request } from "./client";
import { durableMutation } from "./mutations";

export type SupplementStatus = "pending" | "consumed" | "late";

export interface SupplementRecord {
  supplement_id: string;
  run_id: string;
  session_id: string;
  body: string;
  body_version: number;
  status: SupplementStatus;
  consumed_step?: number | null;
}

export interface SupplementReceipt {
  supplement_id: string;
  run_id: string;
  status: SupplementStatus;
  consumed: boolean;
}

export interface SupplementProjection {
  run_id: string;
  supplements: SupplementRecord[];
  pending: number;
}

export type QuestionStatus =
  "waiting" | "answered" | "late" | "closed" | "expired";

export interface QuestionItem {
  text: string;
  choices: string[];
  allow_free_text: boolean;
}

export interface QuestionAnswer {
  choice: string | null;
  text: string | null;
}

export interface QuestionRecord {
  question_id: string;
  run_id: string;
  revision: number;
  questions: QuestionItem[];
  answers?: QuestionAnswer[] | null;
  answer_digest?: string | null;
  answer_status?: string | null;
  closed_reason?: string | null;
  status: QuestionStatus;
}

export interface QuestionProjection {
  run_id: string;
  questions: QuestionRecord[];
  waiting: number;
}

export interface QuestionAnswerReceipt {
  question_id: string;
  run_id: string;
  status: "delivered" | "late";
  duplicate: boolean;
}

export const interactionsApi = {
  async add(
    runId: string,
    body: string,
    bodyVersion = 1,
  ): Promise<SupplementReceipt> {
    return durableMutation<SupplementReceipt>(
      "supplement",
      `/api/runs/${encodeURIComponent(runId)}/supplements`,
      "POST",
      { body, body_version: bodyVersion },
    );
  },
  async list(runId: string): Promise<SupplementProjection> {
    return request<SupplementProjection>(
      `/api/runs/${encodeURIComponent(runId)}/supplements`,
    );
  },
  async listQuestions(runId: string): Promise<QuestionProjection> {
    return request<QuestionProjection>(
      `/api/runs/${encodeURIComponent(runId)}/questions`,
    );
  },
  async answerQuestion(
    runId: string,
    questionId: string,
    answers: QuestionAnswer[],
    questionRevision: number,
  ): Promise<QuestionAnswerReceipt> {
    return durableMutation<QuestionAnswerReceipt>(
      "question-answer",
      `/api/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(questionId)}/answers`,
      "POST",
      { answers, question_revision: questionRevision },
    );
  },
};
