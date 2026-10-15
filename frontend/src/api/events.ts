export interface PreviewEvent {
  type: "model.preview";
  run_id: string;
  request_id?: string;
  revision?: number;
  text: string;
  omitted_chars?: number;
  active: boolean;
  ephemeral: true;
}
export interface RuntimeEvent {
  type: string;
  run_id: string;
  seq: number;
  payload: Record<string, unknown>;
}
export type EventConnection = { close: () => void };
export const eventNames = [
  "run.admitted",
  "skills.snapshot",
  "skill.loaded",
  "run.started",
  "message.user",
  "model.request",
  "model.response",
  "model.retry",
  "model.attempt_started",
  "model.attempt_finished",
  "tool.request",
  "tool.result",
  "tool.prepared",
  "tool.completed",
  "tool.abandoned",
  "tool.outcome_unknown",
  "approval.required",
  "approval.resolved",
  "task.plan_updated",
  "context.compacted",
  "context.trimmed",
  "run.completed",
  "run.failed",
  "run.interrupted",
];
export function connectRun(
  run: string,
  after: number,
  onEvent: (event: RuntimeEvent) => void,
  onPreview: (event: PreviewEvent) => void,
  onError: () => void,
): EventConnection {
  const source = new EventSource(
    `/api/runs/${encodeURIComponent(run)}/events?after=${after}&include_preview=true`,
  );
  for (const name of eventNames)
    source.addEventListener(name, ((event: MessageEvent) => {
      try {
        const value = JSON.parse(event.data);
        if (
          value.run_id === run &&
          Number.isSafeInteger(value.seq) &&
          value.seq > after
        )
          onEvent(value);
      } catch {
        onError();
      }
    }) as EventListener);
  source.addEventListener("model.preview", ((event: MessageEvent) => {
    try {
      const value = JSON.parse(event.data);
      if (
        value.run_id === run &&
        value.type === "model.preview" &&
        value.ephemeral === true &&
        typeof value.active === "boolean" &&
        typeof value.text === "string"
      )
        onPreview(value);
    } catch {
      onError();
    }
  }) as EventListener);
  source.onerror = onError;
  return { close: () => source.close() };
}
