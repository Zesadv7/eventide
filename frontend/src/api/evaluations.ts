import { ApiError, request } from "./client";
import { durableMutation } from "./mutations";
import type { components, paths } from "./schema";

export type EvaluationInput = components["schemas"]["EvaluationBody"];
export type EvaluationSplit = EvaluationInput["split"];
export type EvaluationSuite = components["schemas"]["EvaluationSuite"];
export type EvaluationReceipt = components["schemas"]["EvaluationReceipt"];
export type EvaluationCase = components["schemas"]["EvaluationCase"];
export type EvaluationDetail = components["schemas"]["EvaluationDetail"];
export interface EvaluationReport {
  schema_version: number;
  status: string;
  measurement_kind: "scripted_mechanism_validation";
  billing_applicable: false;
  passed: number;
  total: number;
  attempted: number;
  remaining: number;
  results: unknown[];
  [key: string]: unknown;
}

const prefix = "eventide.evaluation-pending.v1.";
const active = new Set<string>();
const receipts = new Map<string, EvaluationReceipt>();
const listeners = new Set<() => void>();
const pendingKey = (project: string) =>
  `${prefix}${encodeURIComponent(project)}`;

export function evaluationRequestState(project: string) {
  const encoded = sessionStorage.getItem(pendingKey(project));
  let pending: EvaluationInput | null = null;
  if (encoded) {
    try {
      const value = JSON.parse(encoded) as EvaluationInput;
      if (
        value.suite_id === "engineering-v1" &&
        value.mode === "offline" &&
        /^[a-f0-9]{64}$/.test(value.suite_sha256) &&
        ["development", "holdout", "all"].includes(value.split)
      )
        pending = value;
    } catch {
      // Invalid browser state does not become an executable request.
    }
  }
  return {
    active: active.has(project),
    pending,
    receipt: receipts.get(project),
  };
}

export function observeEvaluations(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
function changed() {
  for (const listener of listeners) listener();
}

const suitesPath: keyof paths = "/api/evaluations/suites";
export const evaluationsApi = {
  suites: () => request<EvaluationSuite[]>(suitesPath),
  list: (project: string, before?: string) =>
    request<EvaluationDetail[]>(
      `/api/projects/${encodeURIComponent(project)}/evaluations?limit=20${before ? `&before=${encodeURIComponent(before)}` : ""}`,
    ),
  detail: (id: string) =>
    request<EvaluationDetail>(`/api/evaluations/${encodeURIComponent(id)}`),
  report: (id: string) =>
    request<EvaluationReport>(
      `/api/evaluations/${encodeURIComponent(id)}/report`,
    ),
  cancel: (id: string) =>
    durableMutation<EvaluationDetail>(
      "evaluation-cancel",
      `/api/evaluations/${encodeURIComponent(id)}/cancel`,
      "POST",
      {},
    ),
  async create(
    project: string,
    input: EvaluationInput,
  ): Promise<EvaluationReceipt> {
    if (active.has(project))
      throw new Error("本项目的评测请求正在接收，请等待结果。");
    const body: EvaluationInput = {
      suite_id: input.suite_id,
      suite_sha256: input.suite_sha256,
      split: input.split,
      mode: "offline",
    };
    const pending = evaluationRequestState(project).pending;
    if (pending && JSON.stringify(pending) !== JSON.stringify(body))
      throw new Error("上次评测接收尚未确认，请重试原任务集和划分。");
    sessionStorage.setItem(pendingKey(project), JSON.stringify(body));
    active.add(project);
    changed();
    try {
      const receipt = await durableMutation<EvaluationReceipt>(
        "evaluation",
        `/api/projects/${encodeURIComponent(project)}/evaluations`,
        "POST",
        body,
      );
      receipts.set(project, receipt);
      sessionStorage.removeItem(pendingKey(project));
      return receipt;
    } catch (error) {
      if (
        error instanceof ApiError &&
        ([403, 404, 422].includes(error.status) ||
          error.mutationOutcome === "rejected")
      )
        sessionStorage.removeItem(pendingKey(project));
      throw error;
    } finally {
      active.delete(project);
      changed();
    }
  },
};
