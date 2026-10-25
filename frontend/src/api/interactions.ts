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
};
