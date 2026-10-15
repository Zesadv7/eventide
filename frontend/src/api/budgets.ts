import type { components, paths } from "./schema";
import { request } from "./client";
import { durableMutation } from "./mutations";
import type { UsageLedger } from "./types";

export type BudgetKind = "run" | "project" | "delivery" | "day";
export interface Scope {
  kind: BudgetKind | "session";
  identity: string;
}
export interface Budget {
  kind: BudgetKind;
  identity: string;
  token_limit: number | null;
  actual_tokens: number;
  reserved_tokens: number;
  committed_tokens: number;
  unsettled_attempts: number;
  remaining_tokens: number | null;
  warning: boolean;
  blocked: boolean;
  configuration_hash: string;
}
export interface Reservation {
  attempt_id: string;
  run_id: string;
  session_id: string;
  status: string;
  reserved_tokens: number;
  actual_tokens: number | null;
  usage_source: string;
  input_estimate: number;
  output_estimate: number;
  estimate_method: string;
  reserved_at: string;
  context: {
    project_id: string;
    root_run_id: string;
    delivery_id: string | null;
  };
  audit: Array<Record<string, unknown>>;
}
export interface BudgetStatus {
  timezone: "UTC";
  budgets: Budget[];
  reservations: Reservation[];
}
export type Price = components["schemas"]["PriceBody"];
export interface CostEstimate {
  estimate_id: string;
  attempt_id: string;
  provider: string;
  model: string;
  pricing_at: string;
  usage_source: string;
  complete: boolean;
  unknown_reason: string | null;
  estimates: Record<
    string,
    {
      known_amount: string | null;
      complete: boolean;
      unknown_categories: string[];
      lines: Array<{
        category: string;
        quantity: number;
        price_version: string;
        price_per_million: string;
        amount: string;
      }>;
    }
  >;
}
export interface Costs {
  known_costs: Record<string, string>;
  estimated_usage_costs: Record<string, string>;
  unknown_attempts: number;
  estimated_attempts: number;
  unpriced_attempt_ids: string[];
  historical_unreserved_attempt_ids: string[];
  complete: boolean;
  latest: CostEstimate[];
  history: CostEstimate[];
  usage: UsageLedger;
  attempt_count: number;
  timezone: "UTC";
}
const costsPath: keyof paths = "/api/costs";
const pricesPath: keyof paths = "/api/prices";
const root = (scope: Scope) =>
  `/api/budgets/${scope.kind}/${encodeURIComponent(scope.identity)}`;
export const budgetsApi = {
  status: (scope: Scope) => request<BudgetStatus>(root(scope)),
  configure: (scope: Scope, tokenLimit: number | null, hash: string) =>
    durableMutation<BudgetStatus>("budget", root(scope), "PUT", {
      token_limit: tokenLimit,
      expected_configuration_hash: hash,
    } satisfies components["schemas"]["BudgetBody"]),
  costs: (scope: Scope) =>
    request<Costs>(
      `${costsPath}?${new URLSearchParams({ scope_kind: scope.kind, scope_id: scope.identity, include_children: "true" })}`,
    ),
  prices: () => request<{ prices: Price[] }>(pricesPath),
  registerPrice: (price: Price) =>
    durableMutation<Price>("budget", pricesPath, "POST", price),
  resolve: (attempt: string, body: components["schemas"]["UnknownBody"]) =>
    durableMutation<{ action: string; attempt: Reservation }>(
      "budget",
      `/api/budgets/attempts/${encodeURIComponent(attempt)}/resolve`,
      "POST",
      body,
    ),
};
