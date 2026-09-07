const $ = (selector) => document.querySelector(selector);
const state = { sessionId: null, runId: null, events: 0, source: null };

async function request(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
  return response.json();
}

async function bootstrap() {
  try {
    const health = await request("/healthz");
    $("#health").textContent = health.status.toUpperCase();
    $(".pulse").classList.add("online");
    const session = await request("/api/sessions", { method: "POST" });
    state.sessionId = session.session_id;
    $("#session-id").textContent = state.sessionId;
  } catch (error) {
    $("#health").textContent = "OFFLINE";
  }
}

function compactPayload(event) {
  const payload = { ...event.payload };
  if (payload.text?.length > 500) payload.text = `${payload.text.slice(0, 500)}…`;
  if (payload.content?.length > 500) payload.content = `${payload.content.slice(0, 500)}…`;
  return JSON.stringify(payload, null, 2);
}

function addEvent(event) {
  if (state.events === 0) $("#timeline").replaceChildren();
  state.events += 1;
  $("#event-count").textContent = `${String(state.events).padStart(3, "0")} EVENTS`;
  const row = $("#event-template").content.firstElementChild.cloneNode(true);
  const kind = event.type.split(".")[0];
  row.dataset.kind = event.type.includes("failed") ? "failed" : kind;
  row.querySelector("time").textContent = new Date(event.ts * 1000).toLocaleTimeString([], { hour12: false });
  row.querySelector(".event-title").textContent = event.type.replace(".", " / ");
  row.querySelector(".event-detail").textContent = compactPayload(event);
  if (event.type === "approval.required") {
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    actions.innerHTML = '<button type="button">ALLOW ONCE</button><button type="button" class="deny">DENY</button>';
    const [allow, deny] = actions.querySelectorAll("button");
    allow.onclick = () => resolveApproval(event.payload.approval_id, true, actions);
    deny.onclick = () => resolveApproval(event.payload.approval_id, false, actions);
    row.querySelector(".event-body").append(actions);
  }
  $("#timeline").append(row);
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function resolveApproval(approvalId, approved, container) {
  try {
    await request(`/api/runs/${state.runId}/approvals/${approvalId}`, {
      method: "POST", body: JSON.stringify({ approved }),
    });
    container.innerHTML = approved ? "APPROVED" : "DENIED";
  } catch (error) { container.textContent = error.message; }
}

async function finishRun() {
  const run = await request(`/api/runs/${state.runId}`);
  $("#metric-state").textContent = run.status.toUpperCase();
  $("#metric-steps").textContent = String(run.steps).padStart(2, "0");
  $("#metric-tools").textContent = String(run.tool_calls).padStart(2, "0");
  $("#metric-latency").textContent = `${Math.round(run.duration_ms)}ms`;
  $("#metric-tokens").textContent = Object.values(run.usage).reduce((a, b) => a + b, 0) || "—";
  $("#answer").textContent = run.output || run.error || "No final output.";
  $("#answer-panel").hidden = false;
  $("#run-button").disabled = false;
}

async function execute(prompt) {
  if (!state.sessionId) await bootstrap();
  state.events = 0;
  $("#answer-panel").hidden = true;
  $("#metric-state").textContent = "RUNNING";
  $("#run-button").disabled = true;
  const run = await request(`/api/sessions/${state.sessionId}/runs`, {
    method: "POST", body: JSON.stringify({ prompt }),
  });
  state.runId = run.run_id;
  state.source?.close();
  state.source = new EventSource(`/api/runs/${state.runId}/events`);
  state.source.onmessage = (message) => addEvent(JSON.parse(message.data));
  ["run.started", "model.request", "model.response", "model.retry", "tool.request", "tool.result",
   "approval.required", "approval.resolved", "context.compacted", "run.completed", "run.failed"]
    .forEach(type => state.source.addEventListener(type, (message) => {
      const event = JSON.parse(message.data); addEvent(event);
      if (["run.completed", "run.failed"].includes(type)) { state.source.close(); finishRun(); }
    }));
  state.source.onerror = () => { state.source.close(); finishRun().catch(() => {}); };
}

$("#prompt-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await execute($("#prompt").value.trim()); }
  catch (error) { alert(error.message); $("#run-button").disabled = false; }
});
$("#prompt").addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.key === "Enter") $("#prompt-form").requestSubmit();
});
$("#copy-answer").addEventListener("click", () => navigator.clipboard.writeText($("#answer").textContent));
bootstrap();
