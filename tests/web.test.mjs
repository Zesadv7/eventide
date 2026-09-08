import test from "node:test";
import assert from "node:assert/strict";
import {chapters, mergeEvents, projectWork} from "../nexus_agent/web/projection.js";
import {EventFeed, parseSse} from "../nexus_agent/web/transport.js";

const event = (seq, type, payload, run_id = "r1", extra = {}) => ({event_id: `e${seq}`, run_id, session_seq: seq, type, payload, ...extra});
const prepared = (seq, name, args, id = "c1") => event(seq, "tool.prepared", {call_id: id, name, arguments: args});
const completed = (seq, name, is_error, id = "c1") => event(seq, "tool.completed", {call_id: id, name, is_error, content: is_error ? "Error: failed" : "ok"});
const project = (events) => projectWork([{id: "r1", status: "completed"}], new Map([["r1", events]]));

test("failed pytest stays paired with its command and never claims success", () => {
  const work = project([prepared(1, "bash", {command: "uv run pytest -q"}), completed(2, "bash", true)]);
  assert.equal(work.blocks.length, 1);
  assert.equal(work.blocks[0].kind, "validate");
  assert.match(work.blocks[0].text, /1 项失败/);
  assert.equal(work.operations.get("r1:c1").arguments.command, "uv run pytest -q");
  assert.doesNotMatch(work.blocks[0].text, /完成|通过|no errors/);
});

test("prepared, failed and denied writes are not reported as modifications", () => {
  for (const events of [[prepared(1, "write_file", {path: "a.py"})], [prepared(1, "write_file", {path: "a.py"}), completed(2, "write_file", true)]]) {
    assert.doesNotMatch(project(events).blocks[0].text, /完成|已修改/);
  }
  const work = project([prepared(1, "write_file", {path: "a.py"}), event(2, "approval.required", {approval_id: "a1", tool: "write_file"}), event(3, "approval.resolved", {approval_id: "a1", approved: false}), completed(4, "write_file", true)]);
  assert.equal(work.operations.get("r1:c1").status, "denied");
});

test("checkpoint does not fracture a phase and null is not evidence", () => {
  const work = project([prepared(1, "write_file", {path: "a"}), completed(2, "write_file", false), event(3, "workspace.checkpoint", {checkpoint: null}), prepared(4, "write_file", {path: "b"}, "c2"), completed(5, "write_file", false, "c2")]);
  assert.equal(work.blocks.length, 1);
  assert.equal(work.checkpoint, null);
  assert.equal(work.blocks[0].operations.length, 2);
});

test("aliases, duplicates, disorder and partial facts replay deterministically", () => {
  const events = [prepared(1, "read_file", {path: "README.md"}), completed(2, "read_file", false)];
  const merged = mergeEvents(events, [{...events[1], type: "tool.result"}, event(3, "tool.prepared", {name: "write_file", call_id: "ignore"}, "r1", {partial: true})]);
  assert.equal(merged.length, 3);
  const work = project([...merged].reverse());
  assert.equal(work.operations.size, 1);
  assert.equal(work.blocks[0].operations[0].status, "completed");
});

test("continuation keeps one chapter and resolves abandoned operations across runs", () => {
  const runs = [{id: "r1", status: "interrupted"}, {id: "r2", continuation_of: "r1", status: "interrupted"}, {id: "r3", continuation_of: "r2", status: "completed"}, {id: "r4", status: "completed"}];
  assert.deepEqual(chapters(runs).map((c) => c.runs.length), [3, 1]);
  const map = new Map([["r1", [prepared(1, "read_file", {path: "a"})]], ["r2", [event(2, "tool.abandoned", {name: "read_file", call_id: "c1"}, "r2")]]]);
  const work = projectWork(runs, map);
  assert.equal(work.operations.size, 1);
  assert.equal(work.operations.get("r1:c1").status, "abandoned");
  assert.equal(work.operations.get("r1:c1").events.length, 2);
});

test("same call id in independent runs is not conflated", () => {
  const runs = [{id: "r1"}, {id: "r2"}];
  const work = projectWork(runs, new Map([["r1", [prepared(1, "read_file", {path: "a"})]], ["r2", [{...prepared(2, "read_file", {path: "b"}), run_id: "r2"}]]]));
  assert.equal(work.operations.size, 2);
});

test("interrupted prepared operation has unknown outcome, not ongoing execution", () => {
  const work = projectWork([{id: "r1", status: "interrupted"}], new Map([["r1", [prepared(1, "write_file", {path: "a"})]]]));
  assert.equal(work.operations.get("r1:c1").status, "unknown");
  assert.match(work.blocks[0].text, /结果未确认/);
});

test("advertised but unprepared abandoned call uses its continuation lineage", () => {
  const response = (seq, id, path) => event(seq, "model.response", {message: {content: [{type: "tool_use", id: "same", name: "read_file", input: {path}}]}}, id);
  const runs = [{id: "unrelated"}, {id: "r1", status: "interrupted"}, {id: "r2", continuation_of: "r1"}];
  const work = projectWork(runs, new Map([["unrelated", [response(1, "unrelated", "wrong")]], ["r1", [response(2, "r1", "right")]], ["r2", [event(3, "tool.abandoned", {call_id: "same", name: "read_file"}, "r2")]]]));
  assert.equal(work.operations.get("r1:same").arguments.path, "right");
  assert.equal(work.operations.get("r1:same").status, "abandoned");
});

test("unknown shell commands and model protocol events do not create invented work", () => {
  const work = project([event(1, "model.request", {}), event(2, "model.response", {text: "I changed everything"}), prepared(3, "bash", {command: "echo pytest"})]);
  assert.equal(work.blocks.length, 1);
  assert.equal(work.blocks[0].kind, "tools");
  assert.equal(work.operations.get("r1:c1").status, "prepared");
});

test("SSE parsing preserves canonical sequence and payload", () => {
  const source = event(7, "tool.completed", {content: "中文\nresult"});
  assert.equal(parseSse(`id: 7\nevent: tool.result\ndata: ${JSON.stringify(source)}`).seq, 7);
  assert.equal(parseSse(": keepalive"), null);
});

test("reconnect fills terminal gap from cursor before settling and retains owner", async () => {
  const original = globalThis.fetch;
  const requests = [], owners = [];
  let settled;
  const done = new Promise((resolve) => { settled = resolve; });
  const encode = (e) => `id: ${e.session_seq}\nevent: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`;
  let stream = 0;
  globalThis.fetch = async (url) => {
    requests.push(url);
    if (!url.includes("/events")) return Response.json({id: "r1", status: "completed"});
    stream++;
    if (stream === 1) return new Response(new ReadableStream({start(c) {
      c.enqueue(new TextEncoder().encode(encode(prepared(1, "read_file", {path: "a"}))));
      setTimeout(() => c.error(new Error("offline")), 10);
    }}));
    return new Response(encode(completed(2, "read_file", false)) + encode(event(3, "run.completed", {})));
  };
  try {
    const feed = new EventFeed((owner, e) => { owners.push(owner); if (e.type === "stream.settled") settled(); }, () => {});
    feed.watch("r1", "original-session");
    await Promise.race([done, new Promise((_, reject) => setTimeout(() => reject(new Error("reconnect timeout")), 2500).unref())]);
    assert.equal(feed.events.get("r1").length, 3);
    assert.ok(requests.some((url) => url.endsWith("after=1")));
    assert.ok(owners.every((owner) => owner === "original-session"));
  } finally { globalThis.fetch = original; }
});
