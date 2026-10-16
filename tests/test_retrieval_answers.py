"""Frozen, extractive downstream checks are independent from model self-assessment."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eventide.budget_prices import PriceVersion
from eventide.budgets import BudgetScope
from eventide.models import ModelResponse
from eventide.providers import ScriptedProvider
from eventide.providers.base import ProviderError
from eventide.rag.answer_evaluation import (
    ANSWER_SYSTEM,
    answer_contract,
    answer_passages,
    grade_answer,
)
from eventide.rag.benchmark import load_dataset, run_query_rewrite_benchmark
from eventide.rag.embedding import HashEmbedder
from eventide.rag.evaluation import _profile
from eventide.rag.index import IndexedChunk
from eventide.rag.search import RetrievalOutcome, SearchResult
from tests.test_evaluation_control import host_for

DOCUMENTS = {"doc.txt": "# Heading\nExact frozen evidence\nOther source text\n"}
CASE = {"expect_files": ["doc.txt"], "expect_contains": "frozen"}
PASSAGES = [
    {"path": "doc.txt", "start_line": 1, "end_line": 3, "text": DOCUMENTS["doc.txt"].rstrip("\n")}
]
ANSWER = {
    "answer": "Exact frozen evidence",
    "citations": [
        {"path": "doc.txt", "start_line": 2, "end_line": 2},
    ],
}


def response(value):
    return ModelResponse(json.dumps(value))


def test_exact_answer_requires_real_supplied_lines_and_existing_labels():
    grade = grade_answer(response(ANSWER), case=CASE, documents=DOCUMENTS, passages=PASSAGES)
    assert grade["passed"] and grade["quoted_exactly"] and grade["label_present"]
    assert answer_contract() == answer_contract()
    assert answer_contract()["generation"] == {}
    assert answer_contract()["max_output_tokens"] == 512


@pytest.mark.parametrize(
    "change",
    [
        "fabricated_text",
        "wrong_path",
        "wrong_line",
        "unprovided",
        "heading",
        "duplicate",
        "wrong_label",
        "irrelevant_file",
        "missing_file",
        "fence",
        "extra",
        "boolean_line",
        "reverse_bounds",
        "no_citation",
        "truncated",
        "duplicate_key",
        "nonfinite",
    ],
)
def test_grader_rejects_fabricated_or_unsubstantiated_answers(change):
    answer = json.loads(json.dumps(ANSWER))
    case, passages = CASE, PASSAGES
    if change == "fabricated_text":
        answer["answer"] += " and an invented conclusion"
    elif change == "wrong_path":
        answer["citations"][0]["path"] = "../doc.txt"
    elif change == "wrong_line":
        answer["citations"][0]["end_line"] = 99
    elif change == "unprovided":
        passages = [{**PASSAGES[0], "start_line": 3}]
    elif change == "heading":
        answer = {
            "answer": "# Heading",
            "citations": [
                {"path": "doc.txt", "start_line": 1, "end_line": 1},
            ],
        }
    elif change == "duplicate":
        answer["citations"] *= 2
        answer["answer"] += "\n\n" + answer["answer"]
    elif change == "wrong_label":
        case = {**CASE, "expect_contains": "a fact absent from quoted lines"}
    elif change == "irrelevant_file":
        case = {**CASE, "expect_files": ["other.txt"]}
    elif change == "missing_file":
        case = {**CASE, "expect_files": ["doc.txt", "other.txt"]}
    elif change == "extra":
        answer["model_judge_says_passed"] = True
    elif change == "boolean_line":
        answer["citations"][0]["start_line"] = True
    elif change == "reverse_bounds":
        answer["citations"][0].update(start_line=3, end_line=2)
    elif change == "no_citation":
        answer["citations"] = []
    output = response(answer)
    if change == "fence":
        output = ModelResponse("```json\n" + output.text + "\n```")
    elif change == "truncated":
        output = replace(output, stop_reason="length")
    elif change == "duplicate_key":
        output = ModelResponse('{"answer":"fabricated",' + output.text[1:])
    elif change == "nonfinite":
        output = ModelResponse(output.text.replace('"start_line": 2', '"start_line": NaN'))
    assert not grade_answer(output, case=case, documents=DOCUMENTS, passages=passages)["passed"]


def test_multiple_files_require_exact_ordered_source_quotes_without_inference():
    documents = {**DOCUMENTS, "second.txt": "Another factual excerpt\n"}
    passages = [
        *PASSAGES,
        {"path": "second.txt", "start_line": 1, "end_line": 1, "text": "Another factual excerpt"},
    ]
    answer = {
        "answer": "Exact frozen evidence\n\nAnother factual excerpt",
        "citations": [
            *ANSWER["citations"],
            {"path": "second.txt", "start_line": 1, "end_line": 1},
        ],
    }
    case = {**CASE, "expect_files": ["doc.txt", "second.txt"]}
    assert grade_answer(response(answer), case=case, documents=documents, passages=passages)[
        "passed"
    ]
    answer["citations"].reverse()
    assert not grade_answer(response(answer), case=case, documents=documents, passages=passages)[
        "passed"
    ]


def test_passage_limits_preserve_source_line_numbers_and_never_show_more_than_five():
    documents = {f"{i}.txt": "x" * 600 + "\n" + "y" * 600 for i in range(6)}
    outcome = RetrievalOutcome(
        tuple(
            SearchResult(
                IndexedChunk(
                    i,
                    f"{i}.txt",
                    1,
                    2,
                    "",
                    documents[f"{i}.txt"],
                ),
                1.0,
                (),
            )
            for i in range(6)
        ),
        "test",
    )
    passages = answer_passages(documents, outcome)
    assert len(passages) == 5
    assert all(p["start_line"] == p["end_line"] == 1 for p in passages)
    assert sum(len(p["text"]) for p in passages) == 3000
    with pytest.raises(ValueError, match="outside"):
        answer_passages({}, outcome)
    bad = replace(outcome.results[0].chunk, end_line=99)
    with pytest.raises(ValueError, match="bounds"):
        answer_passages(
            documents, RetrievalOutcome((replace(outcome.results[0], chunk=bad),), "bad")
        )


class AnswerProvider:
    """A mechanism fixture, with label-aware answers; it is not model quality evidence."""

    def __init__(self, mode="valid"):
        self.requests = []
        self.mode = mode
        self.entered = asyncio.Event()

    async def complete(self, request):
        self.requests.append(request)
        if request.system != ANSWER_SYSTEM:
            return ModelResponse(
                "credentials validation lifecycle",
                usage={
                    "input_tokens": 3,
                    "output_tokens": 2,
                }
                if self.mode != "rewrite_usage_unknown"
                else {},
            )
        if self.mode == "network":
            raise ProviderError("private network error", retryable=True)
        if self.mode == "cancel":
            self.entered.set()
            await asyncio.Event().wait()
        if self.mode == "invalid":
            return ModelResponse("not JSON", usage={"input_tokens": 5, "output_tokens": 4})
        payload = json.loads(request.messages[0]["content"])
        dataset = load_dataset()
        case = next(c for c in dataset.cases if c.query == payload["query"])
        quotes, citations = [], []
        for path in case.expect_files:
            passage = next((p for p in payload["passages"] if p["path"] == path), None)
            if passage is None:
                continue
            for offset, line in enumerate(passage["text"].splitlines()):
                if (
                    line.strip()
                    and not line.startswith("#")
                    and (case.expect_contains is None or case.expect_contains in line)
                ):
                    quotes.append(line)
                    line_number = passage["start_line"] + offset
                    citations.append(
                        {"path": path, "start_line": line_number, "end_line": line_number}
                    )
                    break
        return ModelResponse(
            json.dumps({"answer": "\n\n".join(quotes), "citations": citations}),
            usage={"input_tokens": 5, "output_tokens": 4} if self.mode != "usage_unknown" else {},
        )


async def benchmark(host, **kwargs):
    return await run_query_rewrite_benchmark(
        host,
        project_id=host.default_workspace.project_id,
        embedder=HashEmbedder(16),
        profile=_profile(16),
        confirmed=True,
        repeats=1,
        answer_comparison=True,
        **kwargs,
    )


async def test_optional_pair_shares_owner_generation_and_unique_all_attempt_pipeline_costs(
    tmp_path,
):
    host = host_for(tmp_path)
    provider = AnswerProvider()
    host.provider = provider
    try:
        report = await benchmark(host, output_path=tmp_path / "report.json")
        assert report["status"] == "completed" and len(provider.requests) == 60
        assert report["evidence"]["usage"]["attempt_count"] == 60
        assert report["evidence"]["usage"]["reported_totals"] == {
            "input_tokens": 260,
            "output_tokens": 200,
        }
        arms = report["downstream"]["arms"]
        assert arms["original"]["pipeline_usage"]["attempt_count"] == 20
        assert arms["query_rewrite"]["pipeline_usage"]["attempt_count"] == 40
        assert arms["original"]["pipeline_usage"]["reported_totals"] == {
            "input_tokens": 100,
            "output_tokens": 80,
        }
        assert arms["query_rewrite"]["pipeline_usage"]["reported_totals"] == {
            "input_tokens": 160,
            "output_tokens": 120,
        }
        seen = set()
        for arm, summary in arms.items():
            assert summary["total"] == 20 and summary["unattempted"] == 0
            assert summary["pipeline_costs"]["unknown_attempts"] == (
                20 if arm == "original" else 40
            )
            assert summary["cost_per_success"] is None
            for row in summary["results"]:
                assert row["max_output_tokens"] == 512 and row["generation"] == {}
                assert row["input_characters"]["source"] == "characters"
                assert len(row["attempts"]) == 1
                call = row["attempts"][0]
                assert call["kind"] == f"retrieval_answer_{arm}"
                assert call["attempt_id"] not in seen
                seen.add(call["attempt_id"])
                canonical = [
                    e
                    for e in host.store.run_events(row["run_id"])
                    if e["type"] == "retrieval.answer_result"
                ]
                assert row in [e["payload"] for e in canonical]
        assert len(seen) == 40
        assert {r.max_tokens for r in provider.requests if r.system == ANSWER_SYSTEM} == {512}
        assert all(r.generation == {} and r.tools == [] for r in provider.requests)
        assert not report["promotion"]["promote"]
        assert json.loads((tmp_path / "report.json").read_text())["downstream"]["enabled"]
    finally:
        await host.close()


async def test_invalid_answers_continue_all_frozen_queries_without_grader_relaxation(tmp_path):
    host = host_for(tmp_path)
    host.provider = AnswerProvider("invalid")
    try:
        report = await benchmark(host)
        assert report["status"] == "completed" and len(host.provider.requests) == 60
        for arm in report["downstream"]["arms"].values():
            assert arm["total"] == arm["invalid_answers"] == 20
            assert arm["passed"] == arm["failed"] == arm["unattempted"] == 0
    finally:
        await host.close()


@pytest.mark.parametrize("mode", ["network", "usage_unknown", "rewrite_usage_unknown"])
async def test_uncertain_consumption_or_network_stops_expansion_with_all_partial_evidence(
    tmp_path, mode
):
    host = host_for(tmp_path)
    host.provider = AnswerProvider(mode)
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted" and len(report["remaining"]) == 19
        assert len(host.provider.requests) == (1 if mode == "rewrite_usage_unknown" else 2)
        assert report["evidence"]["usage"]["unknown_attempts"] == 1
        arms = report["downstream"]["arms"]
        assert sum(a["total"] for a in arms.values()) == (
            0 if mode == "rewrite_usage_unknown" else 1
        )
        assert sum(a["unattempted"] for a in arms.values()) == (
            40 if mode == "rewrite_usage_unknown" else 39
        )
        assert "private network" not in json.dumps(report)
        assert not report["promotion"]["promote"]
    finally:
        await host.close()


async def test_cancel_during_answer_retains_unknown_attempt_and_partial_pair(tmp_path):
    host = host_for(tmp_path)
    provider = AnswerProvider("cancel")
    host.provider = provider
    output = tmp_path / "partial.json"
    task = asyncio.create_task(benchmark(host, output_path=output))
    try:
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        report = json.loads(output.read_text())
        assert report["status"] == "aborted" and len(report["remaining"]) == 19
        assert report["evidence"]["usage"]["attempt_count"] == 2
        assert report["evidence"]["usage"]["unknown_attempts"] == 1
        rows = [r for a in report["downstream"]["arms"].values() for r in a["results"]]
        assert len(rows) == 1 and rows[0]["status"] == "interrupted"
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        await host.close()


async def test_unresolved_local_dimension_freezes_resolved_fingerprint_before_dispatch(tmp_path):
    host = host_for(tmp_path)
    host.provider = ScriptedProvider([{"error": "stop before expanding"}])
    try:
        report = await run_query_rewrite_benchmark(
            host,
            project_id=host.default_workspace.project_id,
            embedder=HashEmbedder(16),
            profile=replace(_profile(16), dimension=None),
            confirmed=True,
            repeats=1,
        )
        assert report["embedding_fingerprint"] == _profile(16).fingerprint()
        run = report["evidence"]["run_ids"][0]
        accepted = next(
            e["payload"]
            for e in host.store.run_events(run)
            if e["type"] == "control.operation_accepted"
        )
        assert accepted["configuration"]["embedding_fingerprint"] == report["embedding_fingerprint"]
        assert len(host.provider.requests) == 1
    finally:
        await host.close()


async def test_answer_dispatch_uses_same_project_budget_as_rewrite(tmp_path):
    host = host_for(tmp_path)
    provider = AnswerProvider()
    complete = provider.complete

    async def budget_after_rewrite(request):
        result = await complete(request)
        if request.system != ANSWER_SYSTEM:
            host.configure_budget(
                BudgetScope("project", host.default_workspace.project_id),
                token_limit=0,
                request_id="block-answer",
            )
        return result

    provider.complete = budget_after_rewrite
    host.provider = provider
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted" and len(provider.requests) == 1
        assert report["evidence"]["usage"]["attempt_count"] == 1
        answers = [row for arm in report["downstream"]["arms"].values() for row in arm["results"]]
        assert len(answers) == 1 and answers[0]["status"] == "interrupted"
        assert answers[0]["attempts"] == []
    finally:
        await host.close()


@pytest.mark.parametrize("limit, requests", [(5, 1), (14, 2)])
async def test_observed_root_limit_blocks_first_or_second_answer_without_new_attempts(
    tmp_path,
    limit,
    requests,
):
    host = host_for(tmp_path)
    host.settings = replace(host.settings, run_token_limit=limit)
    host.provider = AnswerProvider()
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted"
        assert (
            len(host.provider.requests) == report["evidence"]["usage"]["attempt_count"] == requests
        )
        rows = [row for arm in report["downstream"]["arms"].values() for row in arm["results"]]
        blocked = [row for row in rows if row.get("blocked_before_dispatch")]
        assert len(blocked) == 1 and blocked[0]["reason"] == "observed_run_token_limit"
        assert blocked[0]["status"] == "interrupted" and blocked[0]["attempts"] == []
        run = report["evidence"]["run_ids"][0]
        assert host.store.get_run(run)["status"] == "interrupted"
        assert host.store.run_events(run)[-1]["payload"]["reason"] == "budget_blocked"
        assert len(report["remaining"]) == 19
    finally:
        await host.close()


async def test_fixed_price_pipeline_costs_include_rewrite_once_and_all_failed_answers(tmp_path):
    host = host_for(tmp_path)
    host.provider = AnswerProvider("invalid")
    for category in ("input_tokens", "output_tokens"):
        host.register_price(
            PriceVersion(
                category,
                host.settings.provider,
                host.settings.model,
                category,
                "CNY",
                "1",
                datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
    try:
        report = await benchmark(host)
        costs = report["evidence"]["costs"]
        assert costs["complete"] and Decimal(costs["known_costs"]["CNY"]) == Decimal("0.000460")
        arms = report["downstream"]["arms"]
        assert Decimal(arms["original"]["pipeline_costs"]["known_costs"]["CNY"]) == Decimal(
            "0.000180"
        )
        assert Decimal(arms["query_rewrite"]["pipeline_costs"]["known_costs"]["CNY"]) == Decimal(
            "0.000280"
        )
        assert all(arm["passed"] == 0 and arm["cost_per_success"] is None for arm in arms.values())
        assert len(costs["latest"]) == 60
    finally:
        await host.close()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_answer_receipt_failure_keeps_attempt_cost_and_started_arm_identity(
    tmp_path,
    monkeypatch,
    after_commit,
):
    host = host_for(tmp_path)
    host.provider = AnswerProvider()
    append = host.store.append_event

    def failed(run, kind, payload, *args, **kwargs):
        if kind == "retrieval.answer_result":
            if after_commit:
                append(run, kind, payload, *args, **kwargs)
            raise OSError("answer receipt failed")
        return append(run, kind, payload, *args, **kwargs)

    monkeypatch.setattr(host.store, "append_event", failed)
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted" and len(host.provider.requests) == 2
        assert report["evidence"]["usage"]["attempt_count"] == 2
        arms = report["downstream"]["arms"]
        assert sum(arm["result_missing"] for arm in arms.values()) == (0 if after_commit else 1)
        assert sum(arm["unattempted"] for arm in arms.values()) == 39
        assert sum(arm["pipeline_usage"]["attempt_count"] for arm in arms.values()) == 2
        assert sum(arm["pipeline_costs"]["unknown_attempts"] for arm in arms.values()) == 2
    finally:
        await host.close()


async def test_invalid_structured_answer_is_fully_redacted_after_independent_grading(tmp_path):
    secret = "test-only-api-key-private-value-123456789"
    host = host_for(tmp_path)
    host.settings = replace(host.settings, api_key=secret)

    class SecretProvider(AnswerProvider):
        answered = False

        async def complete(self, request):
            if request.system != ANSWER_SYSTEM:
                return await super().complete(request)
            self.requests.append(request)
            if self.answered:
                raise ProviderError("stop after privacy check")
            self.answered = True
            return ModelResponse(
                json.dumps({"answer": secret, "citations": []}),
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    host.provider = SecretProvider()
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted" and len(host.provider.requests) == 3
        rows = [r for arm in report["downstream"]["arms"].values() for r in arm["results"]]
        graded = next(row for row in rows if row["status"] == "completed")
        assert graded["grade"]["format_valid"] and not graded["grade"]["passed"]
        assert graded["grade"]["answer"]["answer"] != secret
        assert secret not in json.dumps(report)
        assert secret not in json.dumps(host.store.run_events(report["evidence"]["run_ids"][0]))
    finally:
        await host.close()
