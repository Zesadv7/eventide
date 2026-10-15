import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { api, ApiError } from "../api/client";
import {
  connectRun,
  type EventConnection,
  type RuntimeEvent,
  type PreviewEvent,
} from "../api/events";
import type {
  Workspace,
  Session,
  Message,
  Run,
  ProviderStatus,
  RunInput,
  Attachment,
} from "../api/types";
const active = (status?: string) =>
  status === "running" ||
  status === "waiting_for_user" ||
  status === "queued" ||
  status === "accepted";
function stored<T>(key: string, fallback: T): T {
  try {
    return JSON.parse(sessionStorage.getItem(key) || "null") ?? fallback;
  } catch {
    return fallback;
  }
}
export const useWorkbench = defineStore("workbench", () => {
  const workspaces = ref<Workspace[]>([]),
    sessions = ref<Record<string, Session[]>>({});
  const selectedWorkspace = ref(stored<string>("eventide.workspace", "")),
    selectedSession = ref(stored<string>("eventide.session", ""));
  const messages = ref<Record<string, Message[]>>({}),
    runs = ref<Record<string, Run[]>>({}),
    attachments = ref<Record<string, Attachment[]>>({});
  const provider = ref<ProviderStatus | null>(null),
    error = ref(""),
    notice = ref(""),
    connected = ref(true),
    loading = ref(false),
    creating = ref(false);
  const drafts = ref<Record<string, string>>(stored("eventide.drafts", {}));
  const pending = ref<Record<string, { key: string; body: RunInput }>>(
    stored("eventide.pending", {}),
  );
  const sending = ref<Record<string, boolean>>({}),
    previews = ref<Record<string, PreviewEvent>>({}),
    events = ref<Record<string, RuntimeEvent[]>>({}),
    cursors = ref<Record<string, number>>({});
  const currentSessions = computed(
    () => sessions.value[selectedWorkspace.value] || [],
  );
  const currentSession = computed(() =>
    currentSessions.value.find((s) => s.session_id === selectedSession.value),
  );
  const currentRun = computed(
    () =>
      runs.value[selectedSession.value]?.at(-1) ||
      currentSession.value?.latest_run ||
      null,
  );
  const workspaceBusy = computed(() =>
    currentSessions.value.some((s) => active(s.status)),
  );
  let selectionVersion = 0;
  const refreshVersions: Record<string, number> = {};
  let timer: ReturnType<typeof setInterval> | undefined,
    connection: EventConnection | undefined,
    watchedRun = "",
    refreshing = false;
  function persistSelection() {
    sessionStorage.setItem(
      "eventide.workspace",
      JSON.stringify(selectedWorkspace.value),
    );
    sessionStorage.setItem(
      "eventide.session",
      JSON.stringify(selectedSession.value),
    );
  }
  function setDraft(session: string, text: string) {
    drafts.value[session] = text;
    sessionStorage.setItem("eventide.drafts", JSON.stringify(drafts.value));
  }
  function persistPending() {
    sessionStorage.setItem("eventide.pending", JSON.stringify(pending.value));
  }
  function fail(reason: unknown) {
    error.value =
      reason instanceof Error ? reason.message : "请求未完成，请重试";
  }
  async function loadWorkspaces() {
    workspaces.value = await api.workspaces();
    if (
      !workspaces.value.some((w) => w.workspace_id === selectedWorkspace.value)
    )
      selectedWorkspace.value = workspaces.value[0]?.workspace_id || "";
  }
  async function loadSessions(workspace: string) {
    sessions.value[workspace] = await api.sessions(workspace);
  }
  async function refreshSession(session: string) {
    const version = (refreshVersions[session] =
      (refreshVersions[session] || 0) + 1);
    const [status, history, thread, files] = await Promise.all([
      api.session(session),
      api.runs(session),
      api.messages(session).catch((reason) => {
        if (
          reason instanceof ApiError &&
          reason.status === 409 &&
          reason.message.includes("Incomplete tool history")
        )
          return messages.value[session] || [];
        throw reason;
      }),
      api.attachments(session),
    ]);
    const latest = history.at(-1);
    if (latest) history[history.length - 1] = await api.run(latest.run_id);
    if (refreshVersions[session] !== version) return;
    const list = sessions.value[status.workspace_id] || [];
    const position = list.findIndex((s) => s.session_id === session);
    if (position < 0) list.push(status);
    else list[position] = status;
    sessions.value[status.workspace_id] = list;
    runs.value[session] = history;
    messages.value[session] = thread;
    attachments.value[session] = files;
    if (session === selectedSession.value) watchRun();
  }
  function watchRun() {
    const run = currentRun.value;
    if (!run || !active(run.status)) {
      connection?.close();
      connection = undefined;
      watchedRun = "";
      if (run) delete previews.value[run.run_id];
      return;
    }
    if (watchedRun === run.run_id) return;
    connection?.close();
    watchedRun = run.run_id;
    const sessionIdentity = run.session_id || selectedSession.value;
    connection = connectRun(
      run.run_id,
      cursors.value[run.run_id] || 0,
      (event) => {
        const cursor = cursors.value[event.run_id] || 0;
        if (event.seq <= cursor) return;
        cursors.value[event.run_id] = event.seq;
        const history = events.value[event.run_id] || [];
        history.push(event);
        events.value[event.run_id] = history.slice(-200);
        connected.value = true;
        if (event.type === "model.response" || event.type.startsWith("run."))
          delete previews.value[event.run_id];
        void refreshSession(sessionIdentity).catch(fail);
      },
      (preview) => {
        connected.value = true;
        const previous = previews.value[preview.run_id];
        if (
          preview.active &&
          previous?.request_id === preview.request_id &&
          (previous?.revision || 0) > (preview.revision || 0)
        )
          return;
        if (preview.active) previews.value[preview.run_id] = preview;
        else delete previews.value[preview.run_id];
      },
      () => {
        connected.value = false;
      },
    );
  }
  async function selectWorkspace(workspace: string) {
    selectedWorkspace.value = workspace;
    selectedSession.value = "";
    persistSelection();
    await loadSessions(workspace);
    const first = currentSessions.value[0];
    if (first) await selectSession(first.session_id);
    else watchRun();
  }
  async function selectSession(session: string) {
    const version = ++selectionVersion;
    const record = await api.session(session);
    if (version !== selectionVersion) return;
    if (record.workspace_id !== selectedWorkspace.value) {
      selectedWorkspace.value = record.workspace_id;
      await loadSessions(record.workspace_id);
      if (version !== selectionVersion) return;
    }
    selectedSession.value = session;
    persistSelection();
    connection?.close();
    watchedRun = "";
    await refreshSession(session);
  }
  async function initialize() {
    loading.value = true;
    try {
      await loadWorkspaces();
      provider.value = await api.provider();
      if (selectedWorkspace.value) {
        await loadSessions(selectedWorkspace.value);
        if (
          !currentSessions.value.some(
            (s) => s.session_id === selectedSession.value,
          )
        )
          selectedSession.value = currentSessions.value[0]?.session_id || "";
        if (selectedSession.value) await refreshSession(selectedSession.value);
      }
      persistSelection();
    } catch (reason) {
      fail(reason);
    } finally {
      loading.value = false;
    }
  }
  function startPolling() {
    if (timer) return;
    timer = setInterval(async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        const workspace = selectedWorkspace.value;
        if (workspace) await loadSessions(workspace);
        const session = selectedSession.value;
        if (session) await refreshSession(session);
        connected.value = true;
      } catch {
        connected.value = false;
      } finally {
        refreshing = false;
      }
    }, 2500);
  }
  function dispose() {
    if (timer) clearInterval(timer);
    timer = undefined;
    connection?.close();
    connection = undefined;
    watchedRun = "";
  }
  async function createSession() {
    if (!selectedWorkspace.value || creating.value) return;
    creating.value = true;
    const workspace = selectedWorkspace.value;
    try {
      const result = await api.createSession(workspace);
      await loadSessions(workspace);
      await selectSession(result.session_id);
      return result.session_id;
    } finally {
      creating.value = false;
    }
  }
  async function addWorkspace(path: string) {
    const value = await api.addWorkspace(path);
    await loadWorkspaces();
    await selectWorkspace(value.workspace_id);
  }
  async function submit(body: RunInput) {
    const session = selectedSession.value;
    if (!session || sending.value[session]) return;
    let request = pending.value[session];
    if (request && JSON.stringify(request.body) !== JSON.stringify(body))
      throw new Error("上次接收结果尚未确认，请先重试原请求。");
    if (!request) {
      request = { key: crypto.randomUUID(), body };
      pending.value[session] = request;
      persistPending();
    }
    sending.value[session] = true;
    error.value = "";
    try {
      const receipt = await api.submit(session, request.body, request.key);
      delete pending.value[session];
      persistPending();
      setDraft(session, "");
      notice.value = "任务已接收，可切换页面继续工作。";
      await refreshSession(session);
      return receipt;
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        reason.status >= 400 &&
        reason.status < 500 &&
        reason.status !== 408
      ) {
        delete pending.value[session];
        persistPending();
      }
      throw reason;
    } finally {
      sending.value[session] = false;
    }
  }
  async function retryPending() {
    const request = pending.value[selectedSession.value];
    if (request) return submit(request.body);
  }
  async function continueRun() {
    const session = selectedSession.value;
    const key =
      stored<string>(`eventide.continue.${session}`, "") || crypto.randomUUID();
    sessionStorage.setItem(`eventide.continue.${session}`, JSON.stringify(key));
    const receipt = await api.continue(session, key);
    sessionStorage.removeItem(`eventide.continue.${session}`);
    await refreshSession(session);
    return receipt;
  }
  async function cancel() {
    if (currentRun.value) {
      await api.cancel(currentRun.value.run_id);
      await refreshSession(selectedSession.value);
    }
  }
  async function approve(approval: string, approved: boolean) {
    if (currentRun.value) {
      await api.approve(currentRun.value.run_id, approval, approved);
      await refreshSession(selectedSession.value);
    }
  }
  return {
    workspaces,
    sessions,
    selectedWorkspace,
    selectedSession,
    messages,
    runs,
    attachments,
    provider,
    error,
    notice,
    connected,
    loading,
    creating,
    drafts,
    pending,
    sending,
    previews,
    events,
    currentSessions,
    currentSession,
    currentRun,
    workspaceBusy,
    active,
    fail,
    setDraft,
    initialize,
    startPolling,
    dispose,
    selectWorkspace,
    selectSession,
    createSession,
    addWorkspace,
    submit,
    retryPending,
    continueRun,
    cancel,
    approve,
    refreshSession,
  };
});
