"""Opt-in expansion remains bounded, charged and subordinate to original retrieval."""

from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.rag.embedding import provider_from_settings
from eventide.rag.index import IndexedChunk, IndexStore
from eventide.rag.search import RetrievalOutcome, SearchResult
from eventide.retrieval_rewrite import claim_rewrite, merge_queries
from eventide.usage import build_usage_ledger
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def search_call(identity="search"):
    return {"tool_calls": [{"id": identity, "name": "search_knowledge",
                            "arguments": {"query": "verify_token"}}],
            "usage": {"input_tokens": 2, "output_tokens": 1}}


async def run_script(tmp_path, rewrite, *, enabled=True):
    repo = make_repo(tmp_path / "repo")
    (repo / ".git/info/exclude").write_text(".eventide/\n")
    (repo / "doc.txt").write_text("verify_token validates credentials\n")
    settings = replace(settings_for(repo), state_dir=tmp_path / "state", max_tokens=100,
                       retrieval_query_rewrite=enabled)
    embedder, profile = provider_from_settings(settings)
    IndexStore(repo).build(embedder=embedder, profile=profile)
    script = [search_call(), *([rewrite] if enabled else []), search_call("second"),
              {"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}}]
    provider = ScriptedProvider(script)
    host = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        result = await host.run(RunRequest("locate verification", mode="plan"))
        events = host.store.run_events(result.run_id)
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert claim_rewrite(host, result.run_id, "another") is None if enabled else True
        return result, events, ledger, provider.requests
    finally:
        await host.close()


async def test_rewrite_one_attempt_and_supplement_with_original_candidates(tmp_path):
    result, events, ledger, requests = await run_script(tmp_path, {
        "text": "credentials validation", "usage": {"input_tokens": 7, "output_tokens": 2},
    })
    assert result.status == "completed" and len(requests) == 4
    assert requests[1].tools == [] and requests[1].max_tokens == 100
    assert requests[1].generation == {}
    assert ledger["reported_totals"] == {"input_tokens": 14, "output_tokens": 5}
    assert [c["kind"] for c in ledger["calls"]].count("query_rewrite") == 1
    assert sum(e["type"] == "retrieval.rewrite_claimed" for e in events) == 1
    assert sum(e["type"] == "retrieval.supplement_requested" for e in events) == 1
    outputs = [e["payload"]["content"] for e in events if e["type"] == "tool.completed"]
    assert "doc.txt" in outputs[0] and "query_original" in outputs[0]
    assert "rewrite_root_limit_reached" in outputs[1]


@pytest.mark.parametrize("rewrite", [
    {"text": "verify_token"}, {"text": ""}, {"text": "first\nsecond"},
    {"text": "x" * 513}, {"text": "credentials", "stop_reason": "length"},
    search_call("untrusted-tool"),
])
async def test_invalid_rewrite_never_dispatches_supplement_or_tools(tmp_path, rewrite):
    result, events, ledger, requests = await run_script(tmp_path, rewrite)
    assert result.status == "completed" and len(requests) == 4
    assert not any(e["type"] == "retrieval.supplement_requested" for e in events)
    outputs = [e["payload"]["content"] for e in events if e["type"] == "tool.completed"]
    assert len(outputs) == 2 and "rewrite_invalid_or_unchanged" in outputs[0]
    assert "doc.txt" in outputs[0]


async def test_retryable_failure_consumes_single_attempt_and_keeps_unknown_usage(tmp_path):
    result, events, ledger, requests = await run_script(tmp_path, {
        "error": "temporary unavailable", "retryable": True,
    })
    assert result.status == "completed" and len(requests) == 4
    rewrites = [c for c in ledger["calls"] if c["kind"] == "query_rewrite"]
    assert len(rewrites) == 1 and rewrites[0]["usage_source"] == "unknown"
    assert ledger["unknown_attempts"] == 1
    outputs = [e["payload"]["content"] for e in events if e["type"] == "tool.completed"]
    assert "rewrite_failed" in outputs[0] and "doc.txt" in outputs[0]


async def test_default_disabled_does_not_add_attempts(tmp_path):
    result, events, ledger, requests = await run_script(tmp_path, {}, enabled=False)
    assert result.status == "completed" and len(requests) == 3
    assert not any(e["type"].startswith("retrieval.rewrite") for e in events)
    assert not any(c["kind"] == "query_rewrite" for c in ledger["calls"])


def test_rank_fusion_deduplicates_preserves_original_and_discards_incompatible_scores():
    def item(name, score):
        return SearchResult(IndexedChunk(1, name, 1, 2, "", name), score, ("dense",),
                            rerank_score=score, rerank_rank=1)
    original = RetrievalOutcome((item("a.py", 100), item("shared.py", 3)), "reranked")
    extra = RetrievalOutcome((item("shared.py", -10), item("b.py", -100)), "reranked")
    merged = merge_queries(original, extra, 5)
    assert [i.chunk.file for i in merged.results] == ["shared.py", "a.py", "b.py"]
    assert all(i.rerank_score is None and i.rerank_rank is None for i in merged.results)
    assert "query_original" in merged.results[0].sources
    assert "query_rewrite" in merged.results[0].sources


async def test_worker_and_parent_share_claim_after_reopening_store(tmp_path):
    from tests.test_budget_api import new_run

    repo = make_repo(tmp_path / "repo")
    settings = replace(settings_for(repo), state_dir=tmp_path / "state")
    host = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        new_run(host, "root")
        new_run(host, "child", parent="root")
        new_run(host, "sibling", parent="root")
        assert claim_rewrite(host, "child", "private query") == "root"
        assert claim_rewrite(host, "sibling", "different") is None
        assert claim_rewrite(host, "root", "different") is None
        claims = [e for e in host.store.run_events("root")
                  if e["type"] == "retrieval.rewrite_claimed"]
        assert len(claims) == 1 and "private query" not in str(claims)
    finally:
        await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        assert claim_rewrite(reopened, "child", "after restart") is None
    finally:
        await reopened.close()


@pytest.mark.parametrize("failure", ["budget", "cancel", "drift", "search"])
async def test_supplement_failure_and_control_outcomes(tmp_path, monkeypatch, failure):
    import asyncio

    from eventide.budgets import BudgetExceeded, BudgetScope
    from eventide.retrieval_rewrite import with_query_rewrite
    from tests.test_budget_api import new_run

    repo = make_repo(tmp_path / "repo")
    settings = replace(settings_for(repo), state_dir=tmp_path / "state", max_tokens=100)
    provider = ScriptedProvider([{"text": "credentials", "usage": {
        "input_tokens": 2, "output_tokens": 1,
    }}])
    host = RuntimeHost(settings, provider, enable_mcp=False)
    new_run(host, "root")
    generations = iter([1, 1, 2] if failure == "drift" else [1] * 10)
    monkeypatch.setattr(IndexStore, "generation", lambda _: next(generations))
    calls = []
    async def search(query, count):
        calls.append(query)
        if len(calls) > 1:
            raise RuntimeError("untrusted internal detail")
        return RetrievalOutcome((SearchResult(
            IndexedChunk(1, "original.py", 1, 1, "", "original evidence"), 1, ("sparse",),
        ),), "sparse")
    if failure == "budget":
        host.configure_budget(BudgetScope("project", host.default_workspace.project_id),
                              token_limit=0, request_id="no-budget")
    if failure == "cancel":
        async def cancelled(request):
            raise asyncio.CancelledError
        provider.complete = cancelled
    handler = with_query_rewrite(host, "root", repo, search)
    try:
        if failure in {"budget", "cancel"}:
            with pytest.raises(BudgetExceeded if failure == "budget" else asyncio.CancelledError):
                await handler("original")
        else:
            output = await handler("original")
            assert "original.py" in output and "untrusted internal" not in output
        statuses = [e["payload"]["status"] for e in host.store.run_events("root")
                    if e["type"] == "retrieval.rewrite_finished"]
        assert statuses == [{"budget": "budget_blocked", "cancel": "cancelled",
                             "drift": "rewrite_index_changed", "search": "failed"}[failure]]
        assert len(calls) == (2 if failure == "search" else 1)
        assert claim_rewrite(host, "root", "retry") is None
    finally:
        await host.close()
