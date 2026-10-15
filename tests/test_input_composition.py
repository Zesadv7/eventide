"""Final request character composition is exact, private and replayable."""

import asyncio
import json
from collections import Counter
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.input_composition import (
    METHOD,
    measure_input_composition,
    validate_input_composition,
)
from eventide.model_budget import estimate_request
from eventide.models import ModelRequest, RunRequest
from eventide.providers import ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_model_budget import admitted
from tests.test_readonly_workers import WorkerProvider, worker_host
from tests.test_runtime import settings_for
from tests.test_usage_accounting import attempt, event


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def request(messages=None, system="指令🐈\n\"quoted\"\\escaped", tools=None):
    return ModelRequest(system, messages or [], tools or [], "scripted", 100)


def call(identity, name):
    return {"type": "tool_use", "id": identity, "name": name, "input": {"query": "例子"}}


def result(identity, content="结果\n😺", **extra):
    return {"type": "tool_result", "tool_use_id": identity, "content": content, **extra}


def test_unicode_count_partitions_serialized_request_not_bytes_or_tokens():
    item = request([{"role": "user", "content": "中文\n😺\"\\"}], tools=[{
        "name": "read_file", "description": "读取文件", "input_schema": {"type": "object"},
    }])
    measured = measure_input_composition(item)
    counts = measured["components"]
    serialized = json.dumps({"system": item.system, "messages": item.messages,
                             "tools": item.tools}, ensure_ascii=False)
    assert measured["method"] == METHOD and measured["unit"] == "characters"
    assert measured["total_characters"] == len(serialized)
    assert measured["total_characters"] < len(serialized.encode("utf-8"))
    assert counts["instructions"] == size(item.system)
    assert counts["history"] == size(item.messages[0]["content"])
    assert counts["tool_catalog"] == size(item.tools)
    assert counts["envelope"] > 0
    assert sum(counts.values()) == len(serialized)
    assert validate_input_composition(measured) == measured
    assert "中文" not in json.dumps(measured, ensure_ascii=False)


def test_mixed_messages_classify_only_present_tool_pairs():
    calls = [call("skill", "load_skill"), call("resource", "read_skill_resource"),
             call("rag", "search_knowledge"), call("file", "read_file")]
    replies = [result("skill"), result("resource"), result("rag"), result("file"),
               result("orphan", name="load_skill")]
    text = {"type": "text", "text": "My message is not a tool result"}
    item = request([{"role": "assistant", "content": calls},
                    {"role": "user", "content": [text, *replies]}])
    counts = measure_input_composition(item)["components"]
    assert counts["skills"] == size(replies[0]) + size(replies[1])
    assert counts["retrieval"] == size(replies[2])
    assert counts["tool_results"] == size(replies[3]) + size(replies[4])
    assert counts["history"] == sum(map(size, calls)) + size(text)
    assert counts["envelope"] > 0


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": [result("missing", name="load_skill")]}],
    [{"role": "user", "content": [result("late")]},
     {"role": "assistant", "content": [call("late", "load_skill")]}],
    [{"role": "user", "content": [call("wrong-role", "load_skill")]},
     {"role": "user", "content": [result("wrong-role")]}],
    [{"role": "assistant", "content": [call("same", "load_skill"),
                                         call("same", "load_skill")]},
     {"role": "user", "content": [result("same")]}],
    [{"role": "assistant", "content": [call("same", "load_skill"),
                                         {"type": "tool_use", "id": "same", "name": None}]},
     {"role": "user", "content": [result("same")]}],
    [{"role": "assistant", "content": [call("same", "load_skill")]},
     {"role": "user", "content": [result("same")]},
     {"role": "assistant", "content": [call("same", "search_knowledge")]}],
    [{"role": "assistant", "content": [call("same", "load_skill"), result("same")]}],
])
def test_orphan_future_wrong_role_and_ambiguous_results_have_no_invented_source(messages):
    measured = measure_input_composition(request(messages))
    counts = measured["components"]
    assert counts["skills"] == counts["retrieval"] == 0
    assert counts["tool_results"] > 0
    assert sum(counts.values()) == measured["total_characters"]


def test_multimodal_and_provider_blocks_are_counted_without_persisting_their_data():
    image = {"type": "image", "source": {"type": "base64", "data": "private-image-payload"}}
    state = {"type": "provider_state", "provider": "example", "items": [{"secret": "raw-state"}]}
    reply = result("skill", [{"type": "text", "text": "loaded skill"}, image])
    item = request([{"role": "assistant", "content": [call("skill", "load_skill"), state]},
                    {"role": "user", "content": [reply, image, "plain block", None]}])
    measured = measure_input_composition(item)
    assert measured["components"]["skills"] == size(reply)
    assert measured["components"]["history"] == sum(map(size, [
        call("skill", "load_skill"), state, image, "plain block", None,
    ]))
    assert sum(measured["components"].values()) == measured["total_characters"]
    assert "private-image-payload" not in str(measured) and "raw-state" not in str(measured)


def test_summaries_and_unknown_content_shapes_do_not_reconstruct_discarded_history():
    summarized = json.dumps([call("old", "load_skill"), result("old")], ensure_ascii=False)
    messages = [
        {"role": "user", "content": "[Prior context]\n" + summarized},
        {"role": "assistant", "content": {"type": "future", "text": "unknown content"}},
        {"role": "assistant", "extra": {"content": "metadata"}},
    ]
    measured = measure_input_composition(request(messages))
    counts = measured["components"]
    assert counts["skills"] == counts["retrieval"] == counts["tool_results"] == 0
    assert counts["history"] == (
        size(messages[0]["content"]) + size(messages[1]["content"]) + size(messages[2]["extra"])
    )
    assert counts["envelope"] >= 0 and sum(counts.values()) == measured["total_characters"]


def test_top_level_provider_state_is_history_even_without_content():
    state = {"encrypted_content": "opaque-state" * 100}
    message = {"role": "assistant", "provider_state": state, "reasoning_content": "隐藏推理"}
    measured = measure_input_composition(request([message]))
    assert measured["components"]["history"] == size(state) + size("隐藏推理")
    small = measure_input_composition(request([{**message, "provider_state": {}}]))
    assert small["components"]["envelope"] == measured["components"]["envelope"]
    assert sum(measured["components"].values()) == measured["total_characters"]
    assert "opaque-state" not in str(measured) and "隐藏推理" not in str(measured)


def invalid_structures():
    valid = measure_input_composition(request())
    values = [None, {}, [], "0", {**valid, "method": "unknown"}, {**valid, "unit": "tokens"},
              {**valid, "raw_prompt": "must not leak"}, {**valid, "total_characters": 0},
              {**valid, "components": []}, {**valid, "components": {"instructions": 1}}]
    for invalid in (True, -1, 1.5, "1", float("nan"), float("inf")):
        values.append({**valid, "total_characters": invalid})
        value = deepcopy(valid)
        value["components"]["instructions"] = invalid
        values.append(value)
    value = deepcopy(valid)
    value["components"]["raw_prompt"] = "must not leak"
    values.append(value)
    return values


@pytest.mark.parametrize("value", invalid_structures())
def test_projection_strictly_rejects_invalid_or_legacy_composition(value):
    assert validate_input_composition(value) is None
    ledger = build_usage_ledger([event("model.attempt_started", 1, {
        "attempt_id": "one", "input_composition": value,
    })])
    assert ledger["calls"][0]["input_composition"] is None
    assert "must not leak" not in str(ledger)


def test_start_is_the_only_authority_and_replay_never_adds_counts():
    measured = measure_input_composition(request([{"role": "user", "content": "final"}]))
    first = event("model.attempt_started", 1, {
        "request_id": "request", "attempt_id": "try1", "attempt": 1,
        "input_composition": measured,
    })
    finished = attempt(2, input_composition={"raw_prompt": "never from finish"})
    events = [first, finished]
    before = deepcopy(events)
    ledger = build_usage_ledger(events)
    assert ledger["calls"][0]["input_composition"] == measured
    assert build_usage_ledger(events + deepcopy(events)) == ledger
    assert build_usage_ledger(reversed(events)) == ledger
    assert events == before
    conflicting = deepcopy(first)
    conflicting["payload"]["input_composition"] = measure_input_composition(request())
    with pytest.raises(ValueError, match="Conflicting model attempt"):
        build_usage_ledger([first, conflicting])
    assert build_usage_ledger([attempt(input_composition=measured)])["calls"][0][
        "input_composition"
    ] is None
    assert build_usage_ledger([event("model.response", 1, {
        "usage": {"input_tokens": 1}, "input_composition": measured,
    })])["calls"][0]["input_composition"] is None


def test_usage_http_returns_saved_composition_for_run_session_and_costs(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    run_id = admitted(host)
    session = host.store.get_run_identity(run_id)["session_id"]
    item = request([{"role": "user", "content": "private request"}])
    identity = {"request_id": "request", "attempt_id": "one", "attempt": 1,
                "kind": "agent", "provider": "scripted", "model": "scripted"}
    host.model_budget.begin(run_id, item, identity)
    measured = measure_input_composition(item)
    with TestClient(create_app(host)) as client:
        run = client.get(f"/api/runs/{run_id}/usage").json()
        assert run["calls"][0]["input_composition"] == measured
        assert client.get(f"/api/sessions/{session}/usage").json() == run
        costs = client.get("/api/costs", params={"scope_kind": "run", "scope_id": run_id}).json()
        assert costs["usage"] == run
        assert "private request" not in str(run)


async def test_model_budget_begin_persists_only_composition_atomically(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        run_id = admitted(host)
        item = request([{"role": "user", "content": "sensitive-original-text"}])
        identity = {"request_id": "request", "attempt_id": "one", "attempt": 1,
                    "kind": "agent", "provider": "scripted", "model": "scripted"}
        expected = measure_input_composition(item)
        receipt = host.model_budget.begin(run_id, item, identity)
        assert "input_composition" not in identity
        events = host.store.run_events(run_id)
        start = next(e for e in events if e["type"] == "model.attempt_started")
        reserved = next(e for e in events if e["type"] == "budget.reserved")
        assert start["session_seq"] == reserved["session_seq"] + 1
        assert start["payload"]["input_composition"] == expected
        assert "sensitive-original-text" not in str(start["payload"])
        inputs, outputs, method = estimate_request(item)
        assert receipt.reserved_tokens == inputs + outputs
        assert reserved["payload"]["estimate_method"] == method == "serialized_utf8_bytes_v1"
        assert host.model_budget.begin(run_id, item, identity) == receipt
        assert len([e for e in host.store.run_events(run_id)
                    if e["type"] == "model.attempt_started"]) == 1
    finally:
        await host.close()


async def test_agent_and_summary_capture_the_final_requests(isolated_workspace, monkeypatch):
    provider = ScriptedProvider([{"text": "summary"}, {"text": "done"}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)

    async def build(session_id, *, complete_summary, **kwargs):
        await complete_summary(request([{"role": "user", "content": "summarized history"}]))
        return [{"role": "user", "content": "[Prior context]\nsummary"}], None, None

    monkeypatch.setattr(host.context_builder, "build", build)
    try:
        output = await host.run(RunRequest("original input"))
        ledger = build_usage_ledger(host.store.usage_events(run_id=output.run_id))
        assert [row["kind"] for row in ledger["calls"]] == ["context_summary", "agent"]
        assert [row["input_composition"] for row in ledger["calls"]] == [
            measure_input_composition(item) for item in provider.requests
        ]
        assert all(row["input_composition"]["components"]["skills"] == 0
                   for row in ledger["calls"])
    finally:
        await host.close()


async def test_worker_and_parent_capture_independent_final_requests(isolated_workspace):
    provider = WorkerProvider()
    host = worker_host(isolated_workspace, provider)
    try:
        output = await host.run(RunRequest("work"))
        ledger = build_usage_ledger(host.store.usage_events(run_id=output.run_id))
        assert {row["kind"] for row in ledger["calls"]} == {"agent", "worker"}
        assert Counter(json.dumps(row["input_composition"], sort_keys=True)
                       for row in ledger["calls"]) == Counter(
            json.dumps(measure_input_composition(item), sort_keys=True)
            for item in provider.requests
        )
    finally:
        await host.close()


async def test_cancelled_attempt_preserves_composition_through_restart(isolated_workspace):
    entered = asyncio.Event()
    requests = []

    class BlockingProvider:
        async def complete(self, item):
            requests.append(item)
            entered.set()
            await asyncio.Event().wait()

    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, BlockingProvider())
    task = asyncio.create_task(host.run(RunRequest("cancel me", run_id="cancelled-composition")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await host.cancel_run("cancelled-composition")
        with pytest.raises(asyncio.CancelledError):
            await task
        ledger = build_usage_ledger(host.store.usage_events(run_id="cancelled-composition"))
        assert ledger["calls"][0]["input_composition"] == measure_input_composition(requests[0])
        assert ledger["calls"][0]["usage_source"] == "unknown"
        assert ledger["reported_totals"] == {}
    finally:
        await host.close()
    host = RuntimeHost(settings, ScriptedProvider([]))
    try:
        events = host.store.usage_events(run_id="cancelled-composition")
        assert build_usage_ledger(events + events) == ledger
    finally:
        await host.close()
