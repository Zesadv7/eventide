"""Production remote retrieval owns attempts, policy, budgets and cancellation."""

import asyncio
import threading
from dataclasses import replace

import pytest

from eventide import embedding_runtime
from eventide.budgets import BudgetScope
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.policy import PolicyDecision, PolicyEngine, PolicyResult
from eventide.providers import ScriptedProvider
from eventide.rag.embedding import HashEmbedder, provider_from_settings
from eventide.rag.index import IndexStore
from eventide.retrieval_runtime import owned_local_work
from eventide.usage import build_usage_ledger
from tests.test_embedding_runtime import Client, response
from tests.test_host import make_repo
from tests.test_runtime import settings_for


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    (repo / ".git/info/exclude").write_text(".eventide/\n")
    (repo / "doc.txt").write_text("verify_token validates credentials")
    settings = replace(settings_for(repo), state_dir=tmp_path / "state",
                       embedding_provider="openai_compatible", embedding_model="embed-fixture",
                       embedding_api_key="embedding-only-secret", max_tokens=100)
    _, profile = provider_from_settings(settings)
    IndexStore(repo).build(embedder=HashEmbedder(2), profile=profile)
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "search", "name": "search_knowledge",
                         "arguments": {"query": "verify_token"}}],
         "usage": {"input_tokens": 2, "output_tokens": 1}},
        {"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}},
    ])
    host = RuntimeHost(settings, provider, enable_mcp=False)
    client = Client(response())
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    try:
        yield host, provider, client
    finally:
        await host.close()


async def test_host_uses_owned_async_embedding_and_exclusive_ledger(runtime, monkeypatch):
    host, provider, client = runtime
    def forbidden(*args, **kwargs):
        raise AssertionError("unaccounted synchronous embedding adapter")
    monkeypatch.setattr("eventide.rag.embedding.OpenAICompatibleEmbedder.embed", forbidden)
    result = await host.run(RunRequest("find token", mode="plan"))
    assert result.status == "completed" and len(provider.requests) == 2
    assert len(client.requests) == 1 and client.closed
    ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
    assert ledger["reported_totals"] == {"input_tokens": 8, "output_tokens": 2}
    assert ledger["attempt_count"] == 3 and ledger["complete"]
    calls = [call for call in ledger["calls"] if call["kind"] == "embedding"]
    assert len(calls) == 1 and calls[0]["usage_basis"] == "input_only"
    assert calls[0]["usage"] == {"input_tokens": 3}
    tools = [e for e in host.store.run_events(result.run_id) if e["type"] == "tool.completed"]
    assert "doc.txt" in tools[0]["payload"]["content"]
    assert len(host.budget_status()["reservations"]) == 3


async def test_observed_soft_limit_includes_embedding_before_next_generation(runtime):
    host, provider, client = runtime
    host.settings = replace(host.settings, run_token_limit=5)
    result = await host.run(RunRequest("find token", mode="plan"))
    assert result.status == "interrupted" and len(provider.requests) == 1
    assert len(client.requests) == 1 and client.closed
    assert host.store.get_run(result.run_id)["reason"] == "token_budget"


async def test_policy_denial_prevents_embedding_attempt(runtime):
    host, provider, client = runtime
    class Deny(PolicyEngine):
        def evaluate(self, *args, **kwargs):
            return PolicyResult(PolicyDecision.DENY, "fixture denied")
    host.policy = Deny()
    result = await host.run(RunRequest("find token", mode="plan"))
    assert result.status == "completed" and not client.requests
    ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
    assert not any(call["kind"] == "embedding" for call in ledger["calls"])


async def test_embedding_budget_refusal_is_audited_and_parks_without_extra_model(runtime):
    host, provider, client = runtime
    original = host.model_budget.begin_embedding
    def refuse(run_id, texts, identity):
        host.configure_budget(BudgetScope("project", host.default_workspace.project_id),
                              token_limit=0, request_id="lower-before-embedding")
        return original(run_id, texts, identity)
    host.model_budget.begin_embedding = refuse
    result = await host.run(RunRequest("find token", mode="plan"))
    assert result.status == "interrupted" and len(provider.requests) == 1
    assert not client.requests and client.closed
    completed = [e for e in host.store.run_events(result.run_id) if e["type"] == "tool.completed"]
    assert completed[0]["payload"]["metadata"]["code"] == "budget_exceeded"
    assert host.store.get_run(result.run_id)["reason"] == "token_budget"


async def test_cancel_retains_run_until_embedding_client_cleanup(runtime):
    host, provider, client = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    async def pending():
        entered.set()
        await asyncio.Future()
    async def close():
        await release.wait()
    client.request, client.cleanup = pending, close
    session = host.create_session()
    task = asyncio.create_task(host.run(RunRequest("find token", session, mode="plan")))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and host.runtime_busy()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    run = host.store.list_runs(session)[0]
    assert run["status"] == "interrupted" and client.closed
    ledger = build_usage_ledger(host.store.usage_events(run_id=run["id"]))
    assert ledger["unknown_attempts"] == 1
    assert len(provider.requests) == 1


async def test_local_thread_keeps_ownership_on_repeated_cancel():
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        release.wait(3)
        return "done"
    task = asyncio.create_task(owned_local_work(work))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
