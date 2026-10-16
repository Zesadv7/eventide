"""Summary claims bound repeated spending without weakening the context ceiling."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from eventide import context_builder
from eventide.budgets import BudgetExceeded
from eventide.context_builder import SUMMARY_CLAIM, ContextBuilder, ContextOverflow
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from tests.test_runtime import COMPACTION_BUDGET, settings_for


@pytest.fixture
def history(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.create_run("prior", "session")
    store.append_message("session", {"role": "user", "content": "x" * 5000})
    store.finish_run("prior", status="completed")
    store.create_run("active", "session")
    store.append_message("session", {"role": "user", "content": "current goal"})
    yield store
    store.close()


async def build(store, completion, **kwargs):
    return await ContextBuilder(store).build(
        "session",
        provider=ScriptedProvider([]),
        provider_name="scripted",
        model="model",
        budget=20000,
        force=True,
        complete_summary=completion,
        **kwargs,
    )


def complete_turn(store, number, text):
    current = store.latest_run("session")
    store.append_message("session", {"role": "user", "content": text})
    store.finish_run(current["id"], status="completed")
    store.create_run(f"active-{number}", "session")
    store.append_message("session", {"role": "user", "content": "next goal"})


@pytest.mark.parametrize("failure", ["empty", "unadopted", "exception", "truncated"])
async def test_same_prefix_is_never_requested_again_even_when_forced(history, failure):
    responses = []

    async def complete(request):
        responses.append(request)
        if failure == "exception":
            raise RuntimeError("temporary model failure")
        return ModelResponse(
            "" if failure == "empty" else "z" * 20000 if failure == "unadopted" else "partial",
            stop_reason="length" if failure == "truncated" else "stop",
            usage={"input_tokens": 17, "output_tokens": 3},
        )

    await build(history, complete)
    _, compacted, trimmed = await build(history, complete)
    assert len(responses) == 1
    assert trimmed["summary_skipped"] == "same_completed_prefix"
    assert "same_completed_prefix" in compacted["reason"]
    assert history.checkpoints("session") == []
    claims = history.facts_by_type((SUMMARY_CLAIM,))
    assert len(claims) == 1
    assert set(claims[0]["payload"]) == {
        "policy_version",
        "provider",
        "model",
        "covered_seq",
        "prefix_hash",
        "attempted_at",
    }
    assert "usage" not in claims[0]["payload"]
    assert "x" * 100 not in str(claims)


async def test_cooldown_and_new_content_are_both_required_without_sleep(history, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(context_builder.time, "time", lambda: now[0])
    requests = []

    async def complete(request):
        requests.append(request)
        return ModelResponse("")

    await build(history, complete)
    complete_turn(history, 1, "small addition")
    now[0] += 60
    _, _, trimmed = await build(history, complete)
    assert trimmed["summary_skipped"] == "insufficient_completed_content"
    complete_turn(history, 2, "n" * 4000)
    await build(history, complete)
    assert len(requests) == 2
    complete_turn(history, 3, "m" * 4000)
    now[0] += 59
    _, _, trimmed = await build(history, complete)
    assert trimmed["summary_skipped"] == "summary_cooldown"
    now[0] += 1
    await build(history, complete)
    assert len(requests) == 3


async def test_clock_rollback_does_not_remove_cooldown(history, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(context_builder.time, "time", lambda: now[0])

    async def complete(request):
        return ModelResponse("")

    await build(history, complete)
    complete_turn(history, 1, "n" * 4000)
    now[0] = 0
    _, _, trimmed = await build(history, complete)
    assert trimmed["summary_skipped"] == "summary_cooldown"


async def test_same_prefix_claim_survives_store_restart(history):
    async def rejected(request):
        return ModelResponse("")

    await build(history, rejected)
    history.close()
    reopened = RuntimeStore(history.path)
    try:

        async def forbidden(request):
            raise AssertionError("Restart must not spend again")

        _, _, trimmed = await build(reopened, forbidden)
        assert trimmed["summary_skipped"] == "same_completed_prefix"
        assert reopened.checkpoints("session") == []
    finally:
        reopened.close()


async def test_new_prefix_threshold_and_clock_survive_store_restart(history, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(context_builder.time, "time", lambda: now[0])
    requests = []

    async def complete(request):
        requests.append(request)
        return ModelResponse("")

    await build(history, complete)
    complete_turn(history, 1, "small addition")
    history.close()
    reopened = RuntimeStore(history.path)
    try:
        now[0] += 60
        _, _, trimmed = await build(reopened, complete)
        assert trimmed["summary_skipped"] == "insufficient_completed_content"
        complete_turn(reopened, 2, "new content " * 500)
        await build(reopened, complete)
        assert len(requests) == 2
    finally:
        reopened.close()


def test_two_store_writers_cannot_both_claim_the_same_prefix(history):
    competing = RuntimeStore(history.path)
    start = Barrier(2)
    requests = []

    async def complete(request):
        requests.append(request)
        return ModelResponse("")

    def write(store):
        start.wait(timeout=2)
        return asyncio.run(build(store, complete))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(write, history)
            second = pool.submit(write, competing)
            outputs = [first.result(timeout=5), second.result(timeout=5)]
        assert len(requests) == 1
        assert len(history.facts_by_type((SUMMARY_CLAIM,))) == 1
        assert sum(bool(result[2]) for result in outputs) == 1
    finally:
        competing.close()


async def test_competing_builders_claim_once_and_cancel_keeps_original_claim(history):
    entered = asyncio.Event()
    requests = []

    async def complete(request):
        requests.append(request)
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(build(history, complete))
    await asyncio.wait_for(entered.wait(), timeout=1)
    competing = RuntimeStore(history.path)
    try:
        _, _, trimmed = await build(competing, complete)
        assert trimmed["summary_skipped"] == "same_completed_prefix"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _, _, trimmed = await build(competing, complete)
        assert trimmed["summary_skipped"] == "same_completed_prefix"
        assert len(requests) == 1
        assert competing.checkpoints("session") == []
        assert len(competing.facts_by_type((SUMMARY_CLAIM,))) == 1
    finally:
        competing.close()
        task.cancel()


async def test_valid_checkpoint_reuses_result_without_new_claim(history):
    requests = []

    async def complete(request):
        requests.append(request)
        return ModelResponse("retained goal", usage={"input_tokens": 3, "output_tokens": 2})

    messages, compacted, _ = await build(history, complete)
    assert compacted["usage"] == {"input_tokens": 3, "output_tokens": 2}
    repeated, _, _ = await build(history, complete)
    assert repeated == messages
    assert len(requests) == 1
    assert len(history.facts_by_type((SUMMARY_CLAIM,))) == 1


async def test_throttling_never_allows_an_oversize_request(history):
    requests = []

    async def complete(request):
        requests.append(request)
        return ModelResponse("")

    await build(history, complete)
    with pytest.raises(ContextOverflow, match="summary throttled.*same_completed_prefix"):
        await ContextBuilder(history).build(
            "session",
            provider=ScriptedProvider([]),
            provider_name="scripted",
            model="model",
            budget=1000,
            complete_summary=complete,
        )
    assert len(requests) == 1
    assert history.checkpoints("session") == []


async def test_claim_is_scoped_to_session_and_model(history):
    requests = []

    async def complete(request):
        requests.append(request)
        return ModelResponse("")

    await build(history, complete)
    await ContextBuilder(history).build(
        "session",
        provider=ScriptedProvider([]),
        provider_name="scripted",
        model="other",
        budget=20000,
        force=True,
        complete_summary=complete,
    )
    history.create_run("other-prior", "other-session")
    history.append_message("other-session", {"role": "user", "content": "short first prefix"})
    history.finish_run("other-prior", status="completed")
    await ContextBuilder(history).build(
        "other-session",
        provider=ScriptedProvider([]),
        provider_name="scripted",
        model="model",
        budget=20000,
        force=True,
        complete_summary=complete,
    )
    assert len(requests) == 3  # A short initial prefix has no new-content gate.


async def test_budget_refusal_keeps_control_semantics_and_never_becomes_usage(history):
    async def blocked(request):
        raise BudgetExceeded([])

    with pytest.raises(BudgetExceeded):
        await build(history, blocked)
    assert history.checkpoints("session") == []
    assert len(history.facts_by_type((SUMMARY_CLAIM,))) == 1


async def test_cancelled_host_summary_keeps_one_unknown_attempt(isolated_workspace):
    entered = asyncio.Event()

    class Provider:
        requests = 0

        async def complete(self, request):
            self.requests += 1
            if self.requests == 1:
                return ModelResponse("x" * 5000, usage={"input_tokens": 2, "output_tokens": 1})
            entered.set()
            await asyncio.Future()

    provider = Provider()
    host = RuntimeHost(settings_for(isolated_workspace, context_limit=COMPACTION_BUDGET), provider)
    try:
        prior = await host.run(RunRequest("prior"))
        task = asyncio.create_task(host.run(RunRequest("next", prior.session_id)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        summary_events = [
            event
            for event in host.store.session_events(prior.session_id)
            if event["type"] == "model.attempt_started"
            and event["payload"]["kind"] == "context_summary"
        ]
        assert len(summary_events) == 1
        run_id = summary_events[0]["run_id"]
        from eventide.usage import build_usage_ledger

        ledger = build_usage_ledger(host.store.run_events(run_id))
        assert ledger["attempt_count"] == ledger["unknown_attempts"] == 1
        assert ledger["reported_totals"] == {}
        assert host.store.checkpoints(prior.session_id) == []
        with pytest.raises(ContextOverflow, match="summary throttled"):
            await host.context_builder.build(
                prior.session_id,
                provider=provider,
                provider_name=host.settings.provider,
                model=host.settings.model,
                budget=COMPACTION_BUDGET,
            )
        assert provider.requests == 2
    finally:
        await host.close()
