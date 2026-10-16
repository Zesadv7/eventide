import {request} from "./transport.js?v=14";

const active = new Set();
export async function mutateSession(session, action, candidate = {}) {
  const identity = session.session_id;
  const storage = `eventide.legacy-session-mutation.v1.${identity}`;
  if (active.has(identity)) throw new Error("会话管理请求正在进行");
  active.add(identity);
  try {
    let pending;
    const raw = sessionStorage.getItem(storage);
    if (raw !== null) {
      try { pending = JSON.parse(raw); } catch { throw new Error("待确认请求记录损坏，请核对服务端状态"); }
      if (pending.action !== action || JSON.stringify(pending.candidate) !== JSON.stringify(candidate))
        throw new Error("上次请求尚未确认，请恢复原操作后重试");
    } else {
      if (!/^[a-f0-9]{64}$/.test(session.metadata_hash)) throw new Error("请刷新会话后重试");
      pending = {action, candidate, expected: session.metadata_hash, key: crypto.randomUUID()};
      sessionStorage.setItem(storage, JSON.stringify(pending));
    }
    try {
      const response = await request(`/api/sessions/${encodeURIComponent(identity)}`, {
        method: action === "delete" ? "DELETE" : "PATCH",
        headers: {"Content-Type":"application/json", "Idempotency-Key": pending.key},
        body: JSON.stringify({...pending.candidate, expected_metadata_hash:pending.expected}),
      });
      sessionStorage.removeItem(storage);
      return response;
    } catch (error) {
      if ([403,404,422].includes(error.status) || error.mutationOutcome === "rejected") sessionStorage.removeItem(storage);
      throw error;
    }
  } finally { active.delete(identity); }
}

export async function createSession(workspace) {
  const storage = "eventide.legacy-session-create.v1." + workspace;
  const key = sessionStorage.getItem(storage) || crypto.randomUUID();
  sessionStorage.setItem(storage, key);
  try {
    const response = await request("/api/sessions", {
      method: "POST",
      headers: {"Idempotency-Key": key},
      body: JSON.stringify({workspace_id: workspace}),
    });
    sessionStorage.removeItem(storage);
    return response;
  } catch (error) {
    if ([403,404,422].includes(error.status) || error.mutationOutcome === "rejected")
      sessionStorage.removeItem(storage);
    throw error;
  }
}

export async function mutateWorkspace(path, method, body) {
  const fingerprint = JSON.stringify([path, method, body]);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(fingerprint));
  const storage = "eventide.legacy-workspace." + Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
  const key = sessionStorage.getItem(storage) || crypto.randomUUID();
  sessionStorage.setItem(storage, key);
  try {
    const response = await request(path, {method, headers: {"Idempotency-Key": key},
      ...(body ? {body: JSON.stringify(body)} : {})});
    sessionStorage.removeItem(storage);
    return response;
  } catch (error) {
    if ([403,404,422].includes(error.status) || error.mutationOutcome === "rejected")
      sessionStorage.removeItem(storage);
    throw error;
  }
}
