import { durableMutation } from "./mutations";
import type { paths } from "./schema";
import { configurationMutation, generationScope } from "./configuration";
import type {
  Workspace,
  Session,
  Message,
  Run,
  ProviderStatus,
  ProviderInput,
  GenerationState,
  Generation,
  UsageLedger,
  Capabilities,
  Attachment,
  Receipt,
  RunInput,
} from "./types";
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public mutationOutcome?: "rejected",
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    signal: AbortSignal.timeout(30000),
    ...init,
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail))
        message = body.detail
          .map((item: { msg?: string }) => item.msg || "参数错误")
          .join("；");
      else if (typeof body.error?.message === "string")
        message = body.error.message;
    } catch {
      /* HTTP status remains useful */
    }
    throw new ApiError(
      response.status,
      message,
      response.headers?.get("X-Eventide-Mutation-Outcome") === "rejected"
        ? "rejected"
        : undefined,
    );
  }
  return response.json() as Promise<T>;
}
const post = <T>(path: string, body?: unknown, key?: string) =>
  request<T>(path, {
    method: "POST",
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    ...(key ? { headers: { "Idempotency-Key": key } } : {}),
  });
const id = encodeURIComponent;
// This check makes route removal fail typecheck after OpenAPI regeneration.
const workspacePath: keyof paths = "/api/workspaces";
type RawRun = Omit<Run, "run_id"> & { id?: string; run_id?: string };
type RawSession = Omit<Session, "latest_run"> & { latest_run: RawRun | null };
function normalizeRun(value: RawRun): Run {
  const identity = value.run_id || value.id;
  if (!identity) throw new Error("服务返回的运行身份缺失");
  return { ...value, run_id: identity };
}
function normalizeSession(value: RawSession): Session {
  return {
    ...value,
    latest_run: value.latest_run ? normalizeRun(value.latest_run) : null,
  };
}
export const api = {
  workspaces: () => request<Workspace[]>(workspacePath),
  addWorkspace: (path: string) =>
    durableMutation<Workspace>(
      "workspace-register",
      "/api/workspaces",
      "POST",
      { path },
    ),
  sessions: (workspace: string, archived = false) =>
    request<RawSession[]>(
      `/api/workspaces/${id(workspace)}/sessions?include_archived=${archived}`,
    ).then((items) => items.map(normalizeSession)),
  createSession: (workspace: string) =>
    durableMutation<{ session_id: string }>(
      "session-create",
      "/api/sessions",
      "POST",
      { workspace_id: workspace },
    ),
  session: (session: string) =>
    request<RawSession>(`/api/sessions/${id(session)}`).then(normalizeSession),
  messages: (session: string) =>
    request<Message[]>(`/api/sessions/${id(session)}/messages`),
  runs: (session: string, before?: string) =>
    request<RawRun[]>(
      `/api/sessions/${id(session)}/runs?limit=100${before ? `&before=${id(before)}` : ""}`,
    ).then((items) => items.map(normalizeRun)),
  run: (run: string) =>
    request<RawRun>(`/api/runs/${id(run)}`).then(normalizeRun),
  submit: (session: string, body: RunInput, key: string) =>
    post<Receipt>(`/api/sessions/${id(session)}/runs`, body, key),
  continue: (session: string, key: string) =>
    post<Receipt>(`/api/sessions/${id(session)}/continue`, undefined, key),
  abandon: (session: string) =>
    post<Run>(`/api/sessions/${id(session)}/abandon`),
  cancel: (run: string) => post<unknown>(`/api/runs/${id(run)}/cancel`),
  approve: (run: string, approval: string, approved: boolean) =>
    post<unknown>(`/api/runs/${id(run)}/approvals/${id(approval)}`, {
      approved,
    }),
  provider: () => request<ProviderStatus>("/api/config/provider"),
  saveProvider: (body: ProviderInput, expected: string) =>
    configurationMutation<ProviderStatus>(
      "provider",
      "/api/config/provider",
      "PUT",
      {
        provider: body.provider,
        model: body.model.trim(),
        base_url: body.base_url?.trim() || null,
        clear_api_key: body.clear_api_key ?? false,
        ...(body.api_key?.trim() ? { api_key: body.api_key.trim() } : {}),
      },
      expected,
    ),
  resetProvider: (expected: string) =>
    configurationMutation<ProviderStatus>(
      "provider",
      "/api/config/provider",
      "DELETE",
      {},
      expected,
    ),
  models: () =>
    request<{ models: string[]; error: string | null }>("/api/models"),
  generation: (model: string, session?: string) =>
    request<GenerationState>(
      `${session ? `/api/sessions/${id(session)}/generation` : "/api/generation/defaults"}?model=${id(model)}`,
    ),
  saveGeneration: (
    model: string,
    generation: Generation,
    expected: string,
    session?: string,
  ) =>
    configurationMutation<GenerationState>(
      generationScope(model, session),
      session
        ? `/api/sessions/${id(session)}/generation`
        : "/api/generation/defaults",
      "PUT",
      { model, generation },
      expected,
    ),
  usage: (identity: string, scope: "runs" | "sessions") =>
    request<UsageLedger>(`/api/${scope}/${id(identity)}/usage`),
  capabilities: (workspace: string) =>
    request<Capabilities>(`/api/workspaces/${id(workspace)}/capabilities`),
  attachments: (session: string) =>
    request<Attachment[]>(`/api/sessions/${id(session)}/attachments`),
  uploadAttachment: (
    session: string,
    name: string,
    media_type: string,
    content_base64: string,
  ) =>
    durableMutation<Attachment>(
      "attachment-upload",
      `/api/sessions/${id(session)}/attachments`,
      "POST",
      {
        name,
        media_type,
        content_base64,
      },
    ),
  deleteAttachment: (session: string, attachment: string) =>
    durableMutation<unknown>(
      "attachment-delete",
      `/api/sessions/${id(session)}/attachments/${id(attachment)}`,
      "DELETE",
      {},
    ),
};
