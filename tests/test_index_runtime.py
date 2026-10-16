"""Batch builds keep provider ownership and never publish after cancelled inference."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from eventide import embedding_runtime
from eventide.budgets import BudgetExceeded, BudgetScope
from eventide.host import RuntimeHost
from eventide.index_runtime import build_index
from eventide.providers import ScriptedProvider
from eventide.rag.embedding import HashEmbedder, provider_from_settings
from eventide.rag.index import IndexStore
from eventide.usage import build_usage_ledger
from tests.test_budget_api import new_run
from tests.test_embedding_runtime import Client
from tests.test_host import make_repo
from tests.test_runtime import settings_for


@pytest.fixture
async def indexed(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / ".git/info/exclude").write_text(".eventide/\n")
    (repo / "initial.txt").write_text("initial indexed document")
    settings = replace(settings_for(repo), state_dir=tmp_path / "state",
                       embedding_provider="openai_compatible", embedding_model="fixture",
                       embedding_api_key="fixture-secret")
    _, profile = provider_from_settings(settings)
    host = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    new_run(host, "index-run")
    try:
        yield host, repo, settings, profile
    finally:
        await host.close()


def api_response(count):
    return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[1.0, 0.0])
                                 for i in range(count)],
                           usage=SimpleNamespace(prompt_tokens=count * 3))


async def test_batch_split_actual_attempts_and_incremental_no_charge(indexed, monkeypatch):
    host, repo, settings, profile = indexed
    for i in range(130):
        (repo / f"entry-{i}.txt").write_text(f"Document number {i}")
    clients = []
    class DynamicClient(Client):
        async def create(self, **kwargs):
            self.requests.append(kwargs)
            return api_response(len(kwargs["input"]))
    def client_factory(_):
        client = DynamicClient(None)
        clients.append(client)
        return client
    monkeypatch.setattr(embedding_runtime, "_client", client_factory)
    stats = await build_index(host, "index-run", repo, settings=settings, profile=profile)
    lengths = [len(c.requests[0]["input"]) for c in clients]
    assert len(lengths) == 3 and lengths[:2] == [64, 64] and sum(lengths) == stats.chunks
    assert all(c.closed for c in clients)
    ledger = build_usage_ledger(host.store.usage_events(run_id="index-run"))
    assert ledger["attempt_count"] == 3
    assert ledger["reported_totals"]["input_tokens"] == stats.chunks * 3
    await build_index(host, "index-run", repo, settings=settings, profile=profile)
    assert len(clients) == 3


async def test_budget_refusal_preserves_previous_generation(indexed, monkeypatch):
    host, repo, settings, profile = indexed
    IndexStore(repo).build(embedder=HashEmbedder(2), profile=profile)
    previous = IndexStore(repo).generation()
    (repo / "changed.txt").write_text("new input")
    client = Client(api_response(1))
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    host.configure_budget(BudgetScope("project", host.default_workspace.project_id),
                          token_limit=0, request_id="empty-budget")
    with pytest.raises(BudgetExceeded):
        await build_index(host, "index-run", repo, settings=settings, profile=profile)
    assert not client.requests and client.closed
    assert IndexStore(repo).generation() == previous


async def test_repeated_stop_waits_for_provider_cleanup_and_prevents_publish(indexed, monkeypatch):
    host, repo, settings, profile = indexed
    IndexStore(repo).build(embedder=HashEmbedder(2), profile=profile)
    previous = IndexStore(repo).generation()
    (repo / "changed.txt").write_text("new input")
    entered, release = asyncio.Event(), asyncio.Event()
    async def request():
        entered.set()
        await asyncio.Future()
    async def cleanup():
        await release.wait()
    client = Client(None, request=request, close=cleanup)
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    task = asyncio.create_task(build_index(host, "index-run", repo,
                                          settings=settings, profile=profile))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert client.closed and IndexStore(repo).generation() == previous
    ledger = build_usage_ledger(host.store.usage_events(run_id="index-run"))
    assert ledger["unknown_attempts"] == 1
    assert host.budget_status()["reservations"][0]["status"] == "unsettled"


async def test_failed_later_batch_retains_first_charge_and_old_snapshot(indexed, monkeypatch):
    host, repo, settings, profile = indexed
    IndexStore(repo).build(embedder=HashEmbedder(2), profile=profile)
    previous = IndexStore(repo).generation()
    for i in range(70):
        (repo / f"entry-{i}.txt").write_text(f"new document {i}")
    first = Client(api_response(64))
    async def failure():
        raise RuntimeError("supplier detail")
    second = Client(request=failure)
    clients = iter([first, second])
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: next(clients))
    with pytest.raises(embedding_runtime.EmbeddingRequestError):
        await build_index(host, "index-run", repo, settings=settings, profile=profile)
    assert IndexStore(repo).generation() == previous
    ledger = build_usage_ledger(host.store.usage_events(run_id="index-run"))
    assert ledger["reported_totals"] == {"input_tokens": 192}
    assert ledger["unknown_attempts"] == 1 and ledger["attempt_count"] == 2
    assert first.closed and second.closed


async def test_local_cancel_waits_for_cpu_then_refuses_publication(indexed):
    import threading

    host, repo, settings, _ = indexed
    local = replace(settings, embedding_provider="hash", embedding_dim=2)
    embedder, profile = provider_from_settings(local)
    IndexStore(repo).build(embedder=embedder, profile=profile)
    previous = IndexStore(repo).generation()
    (repo / "new.txt").write_text("changed local input")
    entered, release = threading.Event(), threading.Event()
    class SlowEmbedding:
        def embed(self, texts):
            entered.set()
            release.wait(3)
            return HashEmbedder(2).embed(texts)
    task = asyncio.create_task(build_index(host, "index-run", repo, settings=local,
                                          profile=profile, embedder=SlowEmbedding()))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert IndexStore(repo).generation() == previous
    assert build_usage_ledger(host.store.usage_events(run_id="index-run"))["attempt_count"] == 0


async def test_local_build_uses_same_guarded_publication_without_provider(indexed):
    host, repo, settings, _ = indexed
    local = replace(settings, embedding_provider="hash", embedding_dim=2)
    embedder, profile = provider_from_settings(local)
    stats = await build_index(host, "index-run", repo, settings=local,
                              profile=profile, embedder=embedder)
    assert stats.files == 1 and IndexStore(repo).generation() == 1
    assert build_usage_ledger(host.store.usage_events(run_id="index-run"))["attempt_count"] == 0
