import { request } from "./client";

export interface RunToolItem {
  name: string;
  source: string;
  version: string;
  description: string;
  effect: "readonly" | "effect" | string;
  environment: string;
  status: string;
  disabled_reason: string | null;
  provided: boolean;
  schema_status: "full" | "oversize" | string;
  schema_chars: number | null;
  loaded: boolean;
  loaded_seq: number | null;
  load_record: {
    catalog_version?: string;
    schema_digest?: string;
    source?: string;
    effect?: string;
  } | null;
}

export interface RunToolFailure {
  tool: string | null;
  code: string | null;
  reason: string | null;
  seq: number;
  catalog_version: string | null;
}

export interface RunToolCatalog {
  run_id: string;
  catalog_frozen: boolean;
  catalog_version: string | null;
  frozen_seq: number | null;
  total: number;
  catalog_total: number;
  offset: number;
  limit: number;
  items: RunToolItem[];
  loaded_count: number;
  failed_loads: RunToolFailure[];
  stale_receipts: number;
  notes: string[];
}

export interface RunToolDetail extends RunToolItem {
  input_schema: Record<string, unknown> | null;
}

export type RunToolStatusFilter = "all" | "loaded" | "unloaded" | "failed";

export const runToolsApi = {
  catalog(
    runId: string,
    params: {
      query?: string;
      source?: string;
      status?: RunToolStatusFilter;
      offset?: number;
      limit?: number;
    } = {},
  ): Promise<RunToolCatalog> {
    const search = new URLSearchParams();
    if (params.query) search.set("query", params.query);
    if (params.source) search.set("source", params.source);
    if (params.status && params.status !== "all")
      search.set("status", params.status);
    if (params.offset) search.set("offset", String(params.offset));
    if (params.limit) search.set("limit", String(params.limit));
    const suffix = search.toString();
    return request<RunToolCatalog>(
      `/api/runs/${encodeURIComponent(runId)}/tools${suffix ? `?${suffix}` : ""}`,
    );
  },
  detail(runId: string, name: string): Promise<RunToolDetail> {
    return request<RunToolDetail>(
      `/api/runs/${encodeURIComponent(runId)}/tools/${encodeURIComponent(name)}`,
    );
  },
};
