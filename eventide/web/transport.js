import {mergeEvents, terminal} from "./projection.js?v=11";

export async function request(url, options = {}) {
  const response = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* non-JSON error */ }
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export function parseSse(chunk) {
  let type = "message", id, data = [];
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    if (line.startsWith("id:")) id = Number(line.slice(3).trim());
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  const event = JSON.parse(data.join("\n"));
  return {...event, type: type === "message" ? event.type : type, seq: event.session_seq ?? id ?? event.seq};
}

async function readEvents(url, signal, receive) {
  const response = await fetch(url, {signal, headers: {Accept: "text/event-stream"}});
  if (!response.ok) throw new Error(`事件读取失败 (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value, {stream: !done});
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const event = parseSse(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        if (event) receive(event);
      }
      if (done) {
        if (buffer.trim() && !buffer.startsWith(":")) throw new Error("事件流不完整，正在重新读取");
        break;
      }
    }
  } finally { reader.releaseLock(); }
}

export class EventFeed {
  constructor(changed, status) {
    this.events = new Map();
    this.loaded = new Set();
    this.jobs = new Map();
    this.historyJobs = new Map();
    this.changed = changed;
    this.status = status;
  }
  cursor(id) { return Math.max(0, ...(this.events.get(id) || []).map((e) => e.seq || 0)); }
  receive(id, event, owner) {
    this.events.set(id, mergeEvents(this.events.get(id) || [], [event]));
    this.changed(owner, event);
  }
  async history(id, owner) {
    if (this.loaded.has(id)) return;
    if (this.historyJobs.has(id)) return this.historyJobs.get(id);
    const job = readEvents(`/api/runs/${id}/events?after=${this.cursor(id)}`, undefined, (e) => this.receive(id, e, owner))
      .then(() => this.loaded.add(id)).finally(() => this.historyJobs.delete(id));
    this.historyJobs.set(id, job);
    return job;
  }
  watch(id, owner) {
    if (this.jobs.has(id) || this.loaded.has(id)) return;
    const controller = new AbortController();
    const job = {controller, owner, attempts: 0};
    this.jobs.set(id, job);
    const loop = async () => {
      while (!controller.signal.aborted) {
        try {
          await readEvents(`/api/runs/${id}/events?after=${this.cursor(id)}`, controller.signal, (e) => {
            job.attempts = 0;
            this.status(owner, "");
            this.receive(id, e, owner);
          });
          const run = await request(`/api/runs/${id}`, {signal: controller.signal});
          if (terminal(run)) {
            this.loaded.add(id);
            this.status(owner, "");
            this.changed(owner, {type: "stream.settled"});
            break;
          }
          throw new Error("事件连接已结束");
        } catch {
          if (controller.signal.aborted) break;
          this.status(owner, "事件连接暂时中断，正在补齐记录…");
          // Even if the run terminated during disconnection, replay from the durable cursor.
          await new Promise((resolve) => {
            const finish = () => { clearTimeout(timer); controller.signal.removeEventListener("abort", finish); resolve(); };
            const timer = setTimeout(finish, Math.min(5000, 500 * 2 ** job.attempts++));
            controller.signal.addEventListener("abort", finish, {once: true});
          });
        }
      }
      if (this.jobs.get(id) === job) this.jobs.delete(id);
    };
    void loop();
  }
  retain(owners) {
    for (const [id, job] of this.jobs) {
      if (!owners.has(job.owner)) { job.controller.abort(); this.jobs.delete(id); }
    }
  }
}
