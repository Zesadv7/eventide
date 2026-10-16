"""Remote embedding attempts have one owner, bounded dispatch and honest usage."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from eventide import embedding_runtime
from eventide.budgets import BudgetExceeded
from eventide.embedding_runtime import EmbeddingRequestError, embed_remote, embedding_usage
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_model_budget import admitted, project_scope
from tests.test_runtime import settings_for


def response(vectors=None, usage=None):
    return SimpleNamespace(
        data=vectors if vectors is not None else [SimpleNamespace(index=0, embedding=[1., 2.])],
        usage=usage if usage is not None else SimpleNamespace(prompt_tokens=3, total_tokens=3),
    )


class Client:
    def __init__(self, result=None, *, request=None, close=None):
        self.result = result or response()
        self.embeddings = self
        self.requests = []
        self.closed = False
        self.request = request
        self.cleanup = close

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return await self.request() if self.request else self.result

    async def close(self):
        if self.cleanup:
            await self.cleanup()
        self.closed = True


@pytest.fixture
async def remote_host(isolated_workspace):
    settings = replace(
        settings_for(isolated_workspace), embedding_provider="openai_compatible",
        embedding_api_key="fixture-embedding-secret", embedding_model="embedding-model",
        embedding_base_url="https://embedding.invalid/v1", model_timeout=1,
    )
    host = RuntimeHost(settings, ScriptedProvider([]))
    yield host
    await host.close()


def ledger(host, run):
    return build_usage_ledger(host.store.usage_events(run_id=run))


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 0, "total_tokens": 0}, {"input_tokens": 0}),
    ({"prompt_tokens": 4, "total_tokens": 4}, {"input_tokens": 4}),
    ({"prompt_tokens": 4}, {"input_tokens": 4}),
    ({"total_tokens": 4}, {}), (None, {}), ({}, {}),
    ({"prompt_tokens": 4, "total_tokens": 8}, {}),
    ({"prompt_tokens": True}, {}), ({"prompt_tokens": -1}, {}),
    ({"prompt_tokens": "4"}, {}), ({"prompt_tokens": 4.0}, {}),
    ({"prompt_tokens": 4, "total_tokens": True}, {}),
])
def test_embedding_protocol_does_not_add_or_invent_counters(usage, expected):
    assert embedding_usage(usage) == expected


async def test_request_orders_vectors_and_records_one_attempt(remote_host, monkeypatch):
    host = remote_host
    client = Client(response(vectors=[
        {"index": 1, "embedding": [3., 4.]}, {"index": 0, "embedding": [1., 2.]},
    ]))
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    run = admitted(host)
    assert await embed_remote(host, run, ["first", "second"]) == [[1., 2.], [3., 4.]]
    assert client.requests == [{"model": "embedding-model", "input": ["first", "second"],
                                "encoding_format": "float"}]
    assert client.closed
    usage = ledger(host, run)
    assert usage["attempt_count"] == 1
    assert usage["reported_totals"] == {"input_tokens": 3}
    assert usage["complete"]
    assert usage["calls"][0]["kind"] == "embedding"
    assert host.budget_status()["reservations"][0]["actual_tokens"] == 3


@pytest.mark.parametrize("vectors", [
    [], [{"index": 1, "embedding": [1.]}],
    [{"index": True, "embedding": [1.]}], [{"index": 0, "embedding": []}],
    [{"index": 0, "embedding": [float("nan")]}],
    [{"index": 0, "embedding": [True]}], [{"index": 0, "embedding": ["1"]}],
])
async def test_unusable_response_still_settles_reported_cost(remote_host, monkeypatch, vectors):
    client = Client(response(vectors=vectors))
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    run = admitted(remote_host)
    with pytest.raises(EmbeddingRequestError):
        await embed_remote(remote_host, run, ["query"])
    assert client.closed
    usage = ledger(remote_host, run)
    assert usage["reported_totals"] == {"input_tokens": 3}
    assert usage["calls"][0]["status"] == "failed"
    assert remote_host.budget_status()["reservations"][0]["status"] == "settled"


async def test_failure_is_not_retried_and_does_not_leak_sdk_text(remote_host, monkeypatch):
    async def request():
        raise RuntimeError("fixture-embedding-secret raw provider request query")
    client = Client(request=request)
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    run = admitted(remote_host)
    with pytest.raises(EmbeddingRequestError) as failed:
        await embed_remote(remote_host, run, ["query"])
    assert "secret" not in str(failed.value)
    assert failed.value.__suppress_context__
    assert len(client.requests) == 1
    assert client.closed
    assert ledger(remote_host, run)["unknown_attempts"] == 1
    assert "fixture-embedding-secret" not in str(remote_host.store.run_events(run))
    assert remote_host.budget_status()["reservations"][0]["status"] == "unsettled"


async def test_budget_refusal_precedes_remote_call(remote_host, monkeypatch):
    client = Client()
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    remote_host.configure_budget(project_scope(remote_host), token_limit=0, request_id="blocked")
    run = admitted(remote_host)
    with pytest.raises(BudgetExceeded):
        await embed_remote(remote_host, run, ["query"])
    assert not client.requests and client.closed
    assert ledger(remote_host, run)["attempt_count"] == 0
    assert remote_host.budget_status()["reservations"] == []


async def test_waiting_for_shared_slot_cancellation_never_reserves(remote_host, monkeypatch):
    created = []
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: created.append(Client()))
    run = admitted(remote_host)
    async with (
        remote_host.dispatch.scheduler.model_slot(),
        remote_host.dispatch.scheduler.model_slot(),
    ):
        task = asyncio.create_task(embed_remote(remote_host, run, ["query"]))
        await asyncio.sleep(.01)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert created == []
    assert ledger(remote_host, run)["attempt_count"] == 0
    assert remote_host.budget_status()["reservations"] == []


@pytest.mark.parametrize("late_usage", [False, True])
async def test_repeated_cancel_waits_request_and_client_cleanup(
    remote_host, monkeypatch, late_usage,
):
    entered, cleaning, release, closed, close_release = (asyncio.Event() for _ in range(5))
    async def request():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            if late_usage:
                return response()
            raise
    async def close():
        closed.set()
        await close_release.wait()
    client = Client(request=request, close=close)
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    run = admitted(remote_host)
    task = asyncio.create_task(embed_remote(remote_host, run, ["query"]))
    await entered.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done() and not closed.is_set()
    release.set()
    await closed.wait()
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and len(client.requests) == 1
    usage = ledger(remote_host, run)
    assert usage["calls"][0]["status"] == "cancelled"
    assert usage["reported_totals"] == ({"input_tokens": 3} if late_usage else {})
    assert usage["unknown_attempts"] == (0 if late_usage else 1)


async def test_timeout_waits_cleanup_and_retains_unknown(remote_host, monkeypatch):
    async def request():
        await asyncio.Event().wait()
    client = Client(request=request)
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    run = admitted(remote_host)
    with pytest.raises(TimeoutError, match="Embedding request timed out"):
        await embed_remote(remote_host, run, ["query"],
                           settings=replace(remote_host.settings, model_timeout=.01))
    assert client.closed
    assert ledger(remote_host, run)["unknown_attempts"] == 1


async def test_close_failure_keeps_actual_usage(remote_host, monkeypatch):
    async def close():
        raise RuntimeError("fixture-embedding-secret")
    client = Client(close=close)
    monkeypatch.setattr(embedding_runtime, "_client", lambda _: client)
    run = admitted(remote_host)
    with pytest.raises(EmbeddingRequestError, match="client cleanup failed"):
        await embed_remote(remote_host, run, ["query"])
    assert ledger(remote_host, run)["reported_totals"] == {"input_tokens": 3}
    assert ledger(remote_host, run)["calls"][0]["status"] == "failed"


def test_sdk_factory_disables_implicit_retries_and_pins_timeout(remote_host, monkeypatch):
    import openai
    captured = {}
    def factory(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    embedding_runtime._client(remote_host.settings)
    assert captured["max_retries"] == 0
    assert captured["timeout"] == remote_host.settings.model_timeout
    assert captured["base_url"] == "https://embedding.invalid/v1"


@pytest.mark.parametrize("texts", [[], [""], [" "], [7], ["query"] * 65,
                                   ["a" * (1024 * 1024 + 1)]])
async def test_invalid_inputs_do_not_dispatch(remote_host, monkeypatch, texts):
    def unexpected(_):
        raise AssertionError("Client must not be constructed")
    monkeypatch.setattr(embedding_runtime, "_client", unexpected)
    run = admitted(remote_host)
    with pytest.raises(ValueError):
        await embed_remote(remote_host, run, texts)
    assert ledger(remote_host, run)["attempt_count"] == 0
