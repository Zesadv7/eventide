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
export interface ConnectionStatus {
  state: "connecting" | "connected" | "retrying" | "offline" | "closed";
  attempt: number;
  nextRetryAt: number | null;
}
export const eventNames = [
  "run.admitted",
  "skills.snapshot",
  "skill.loaded",
  "run.started",
  "retrieval.status",
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
  onStatus: (status: ConnectionStatus) => void = () => {},
): EventConnection {
  let cursor = after;
  let source: EventSource | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let failures = 0;
  let openedAt: number | undefined;
  let closed = false;

  function status(
    state: ConnectionStatus["state"],
    nextRetryAt: number | null = null,
  ) {
    onStatus({ state, attempt: failures, nextRetryAt });
  }
  function disconnect() {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    source?.close();
    source = undefined;
  }
  function close() {
    if (closed) return;
    closed = true;
    disconnect();
    window.removeEventListener("online", wake);
    window.removeEventListener("offline", offline);
    document.removeEventListener("visibilitychange", visible);
    status("closed");
  }
  function retry() {
    if (openedAt !== undefined && Date.now() - openedAt >= 10_000) failures = 0;
    openedAt = undefined;
    disconnect();
    failures += 1;
    onError();
    if (!navigator.onLine) {
      status("offline");
      return;
    }
    const base = Math.min(500 * 2 ** Math.min(failures - 1, 7), 30_000);
    const delay = Math.ceil(base * (1 + Math.random() * 0.25));
    status("retrying", Date.now() + delay);
    timer = setTimeout(open, delay);
  }
  function open() {
    if (closed) return;
    disconnect();
    if (!navigator.onLine) {
      status("offline");
      return;
    }
    status("connecting");
    const current = new EventSource(
      `/api/runs/${encodeURIComponent(run)}/events?after=${cursor}&include_preview=true`,
    );
    source = current;
    current.onopen = () => {
      if (closed || source !== current) return;
      openedAt = Date.now();
      status("connected");
    };
    current.onerror = () => {
      if (!closed && source === current) retry();
    };
    for (const name of eventNames) {
      current.addEventListener(name, ((event: MessageEvent) => {
        if (closed || source !== current) return;
        let value: RuntimeEvent;
        try {
          value = JSON.parse(event.data);
        } catch {
          retry();
          return;
        }
        if (
          !value ||
          value.run_id !== run ||
          value.type !== name ||
          !Number.isSafeInteger(value.seq) ||
          value.seq <= cursor
        )
          return;
        onEvent(value);
        cursor = value.seq;
        if (["run.completed", "run.failed", "run.interrupted"].includes(name))
          close();
      }) as EventListener);
    }
    current.addEventListener("model.preview", ((event: MessageEvent) => {
      if (closed || source !== current) return;
      let value: PreviewEvent;
      try {
        value = JSON.parse(event.data);
      } catch {
        retry();
        return;
      }
      if (
        value &&
        value.run_id === run &&
        value.type === "model.preview" &&
        value.ephemeral === true &&
        typeof value.active === "boolean" &&
        typeof value.text === "string"
      )
        onPreview(value);
    }) as EventListener);
  }
  function wake() {
    if (!closed && !source) open();
  }
  function offline() {
    if (closed) return;
    disconnect();
    openedAt = undefined;
    onError();
    status("offline");
  }
  function visible() {
    if (document.visibilityState === "visible") wake();
  }
  window.addEventListener("online", wake);
  window.addEventListener("offline", offline);
  document.addEventListener("visibilitychange", visible);
  open();
  return { close };
}
