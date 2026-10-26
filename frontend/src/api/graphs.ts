import { request } from "./client";
import { durableMutation } from "./mutations";

export interface GraphNodeInput {
  id: string;
  kind: "readonly";
  goal: string;
  depends_on: string[];
}

export interface GraphDefinition {
  title: string;
  nodes: GraphNodeInput[];
}

export interface GraphNode extends GraphNodeInput {
  state: string;
  claim: {
    graph_id: string;
    node_id: string;
    run_id: string;
    child_session_id?: string;
  } | null;
  result: {
    run_id: string;
    status: string;
    output: string;
    output_truncated?: boolean;
    output_length?: number;
    error: string | null;
    child_session_id?: string;
  } | null;
}

export interface AgentGraph {
  graph_id: string;
  root_run_id: string;
  session_id: string;
  title: string;
  state: string;
  cancelled: boolean;
  started: boolean;
  nodes: GraphNode[];
  counts: { total: number; running: number; completed: number };
}

export const graphsApi = {
  list(runId: string) {
    return request<{ run_id: string; graphs: AgentGraph[] }>(
      `/api/runs/${encodeURIComponent(runId)}/graphs`,
    );
  },
  get(graphId: string) {
    return request<AgentGraph>(`/api/graphs/${encodeURIComponent(graphId)}`);
  },
  create(runId: string, definition: GraphDefinition) {
    return durableMutation<AgentGraph>(
      "graph-create",
      `/api/runs/${encodeURIComponent(runId)}/graphs`,
      "POST",
      definition,
    );
  },
  advance(graphId: string) {
    return durableMutation<AgentGraph>(
      "graph-advance",
      `/api/graphs/${encodeURIComponent(graphId)}/advance`,
      "POST",
      {},
    );
  },
  cancel(graphId: string) {
    return durableMutation<AgentGraph>(
      "graph-cancel",
      `/api/graphs/${encodeURIComponent(graphId)}/cancel`,
      "POST",
      {},
    );
  },
};
