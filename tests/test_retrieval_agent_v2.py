"""Independently reviewed answers and failure distinctions for the v2 task contract."""

import ast
import copy
import hashlib
import json
from importlib import resources

import pytest
from pydantic import ValidationError

from eventide.rag.agent_evaluation import (
    DATASET_SHA256,
    DATASET_V2_SHA256,
    InvestigationCaseV2,
    InvestigationDatasetV2,
    attempt_plan,
    grade_investigation,
    investigation_prompt,
    load_agent_dataset,
)

# Values and citation choices were written from the source, not generated from Fact labels.
# Quote text comes from those separately selected source ranges to preserve exact whitespace.
REVIEWED_ANSWERS = {
    "notice-idempotency": (
        {"deduplication_key": "event.delivery_key", "storage_method": "record_delivery"},
        [("deduplication_key", "billing/ingress.py", 7, 7),
         ("storage_method", "billing/ingress.py", 12, 13)],
    ),
    "catalog-restore-window": (
        {"transition_function": "retire_notebook", "deadline_attribute": "record.restore_until"},
        [("transition_function", "garden/catalog.py", 4, 13),
         ("deadline_attribute", "garden/catalog.py", 8, 11)],
    ),
    "parcel-cover-chain": (
        {"coverage_setting": "settings.coverage_ceiling", "coverage_exception": "UncoveredValue"},
        [("coverage_setting", "lantern/intake.py", 5, 11),
         ("coverage_exception", "lantern/insurance.py", 4, 9)],
    ),
    "retry-current-checkout": (
        {"delay_minutes": "10", "eligibility_expression":
         'attempt.state == "failed" and attempt.tries < policy.retry_ceiling'},
        [("delay_minutes", "harbor/retries.py", 1, 2),
         ("eligibility_expression", "harbor/retries.py", 4, 5)],
    ),
    "archive-page-cursor": (
        {"reader_argument": "anchor", "initial_bookmark": "archive-start"},
        [("reader_argument", "loom/windows.py", 7, 8),
         ("initial_bookmark", "loom/windows.py", 4, 11)],
    ),
    "broadcast-final-drain": (
        {"shutdown_function": "retire_stream", "buffer_operation": "stream.buffer.flush_pending"},
        [("shutdown_function", "wave/lifecycle.py", 3, 13),
         ("buffer_operation", "wave/lifecycle.py", 7, 10)],
    ),
    "report-signing-chain": (
        {"signing_function": "seal_report", "expired_key_exception": "SigningKeyExpired"},
        [("signing_function", "cedar/publisher.py", 5, 11),
         ("expired_key_exception", "cedar/signatures.py", 4, 8)],
    ),
    "export-copy-profile": (
        {"format_name": "png", "alpha_policy": "preserve"},
        [("format_name", "mosaic/export_profile.py", 4, 7),
         ("alpha_policy", "mosaic/export_profile.py", 4, 7)],
    ),
}


def reviewed_answer(case):
    facts, citations = REVIEWED_ANSWERS[case.id]
    sources = {**case.documents, **case.after_index}
    return {
        "facts": dict(facts),
        "citations": [
            {"fact": key, "path": path, "start_line": start, "end_line": end,
             "quote": "\n".join(sources[path].splitlines()[start - 1:end])}
            for key, path, start, end in citations
        ],
    }


def test_versions_keep_distinct_frozen_sources_and_pair_counts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = resources.files("eventide").joinpath(
        "bundled_evals/retrieval-agent-v1/dataset.json").read_bytes()
    new = resources.files("eventide").joinpath(
        "bundled_evals/retrieval-agent-v2/dataset.json").read_bytes()
    assert hashlib.sha256(old).hexdigest() == DATASET_SHA256
    assert hashlib.sha256(new).hexdigest() == DATASET_V2_SHA256
    assert len(attempt_plan(load_agent_dataset(), "holdout")) == 16
    dataset = load_agent_dataset("v2")
    assert dataset.version == "retrieval-agent-v2"
    for split in ("development", "holdout"):
        plan = attempt_plan(dataset, split)
        assert len(plan) == 8
        assert {slot["repetition"] for slot in plan} == {0}
        assert [slot["arm"] for slot in plan[:4]] == [
            "baseline", "candidate", "candidate", "baseline"]
    development = [case for case in dataset.cases if case.split == "development"]
    holdout = [case for case in dataset.cases if case.split == "holdout"]
    assert not {case.project for case in development} & {case.project for case in holdout}
    assert not {case.family for case in development} & {case.family for case in holdout}
    for case in dataset.cases:
        assert len(case.documents) == 10
        assert sum(len(text.splitlines()) for text in case.documents.values()) >= 70
        for sources in (case.documents, case.after_index, case.foreign_documents):
            for path, text in sources.items():
                if path.endswith(".py"):
                    ast.parse(text, filename=path)
    with pytest.raises(ValueError, match="v1 or v2"):
        load_agent_dataset("v3")


@pytest.mark.parametrize("case", load_agent_dataset("v2").cases, ids=lambda case: case.id)
def test_independently_reviewed_answers_pass_and_prompt_has_actual_fields(case):
    answer = reviewed_answer(case)
    grade = grade_investigation(json.dumps(answer), case, {**case.documents, **case.after_index})
    assert grade["passed"] and grade["schema_valid"] and grade["field_keys_correct"]
    assert grade["correct_fact_count"] == grade["present_fact_count"] == 2
    assert grade["authentic_citations"] == grade["valid_citations"] == 2
    assert not grade["failure_reasons"]
    prompt = investigation_prompt(case)
    assert "requested_key" not in prompt
    assert "without call parentheses or await" in prompt or "bare Python name" in prompt or (
        "string literal contents" in prompt or "integer literal" in prompt)
    for key in answer["facts"]:
        assert f'"{key}"' in prompt and f'"fact": "{key}"' in prompt
    assert "alternative_evidence" not in prompt and "foreign_documents" not in prompt
    assert "search_knowledge" not in prompt  # Choosing retrieval remains independent of grading.


def test_handwritten_notice_payload_and_reviewed_storage_definition_are_both_supported():
    case = load_agent_dataset("v2").cases[0]
    answer = {
        "facts": {"deduplication_key": "event.delivery_key", "storage_method": "record_delivery"},
        "citations": [
            {"fact": "deduplication_key", "path": "billing/ingress.py",
             "start_line": 7, "end_line": 7,
             "quote": "    previous = ledger.lookup_delivery(event.delivery_key)"},
            {"fact": "storage_method", "path": "billing/ledger.py",
             "start_line": 16, "end_line": 20,
             "quote": "    def record_delivery(self, delivery_key, invoice_id, "
                      "*, status, amount):\n"
                      "        row = DeliveryRow(invoice_id, status, amount)\n"
                      "        self.database.deliveries[delivery_key] = row\n"
                      "        self.database.flush()\n"
                      "        return row"},
        ],
    }
    grade = grade_investigation(json.dumps(answer), case, case.documents)
    assert grade["passed"] and grade["authentic_citations"] == 2


@pytest.mark.parametrize("identity,path,end", [
    ("notice-idempotency", "billing/ingress.py", 15),
    ("report-signing-chain", "cedar/publisher.py", 11),
])
def test_real_supporting_citation_can_include_surrounding_imports(identity, path, end):
    case = next(case for case in load_agent_dataset("v2").cases if case.id == identity)
    answer = reviewed_answer(case)
    answer["citations"][0].update(
        path=path, start_line=1, end_line=end,
        quote="\n".join(case.documents[path].splitlines()[:end]),
    )
    grade = grade_investigation(json.dumps(answer), case, case.documents)
    assert grade["passed"] and grade["valid_citations"] == 2


def test_value_only_outside_reviewed_intersection_does_not_bind():
    case = load_agent_dataset("v2").cases[0]
    answer = reviewed_answer(case)
    documents = dict(case.documents)
    # A true comment is not support for what the reviewed function actually does.
    lines = documents["billing/ingress.py"].splitlines()
    lines[0] = "# event.delivery_key is discussed elsewhere"
    documents["billing/ingress.py"] = "\n".join(lines) + "\n"
    answer["citations"][0].update(start_line=1, end_line=4, quote="\n".join(lines[:4]))
    grade = grade_investigation(json.dumps(answer), case, documents)
    assert grade["authentic_citations"] == 2 and not grade["passed"]
    assert grade["citation_results"][0]["reason"] == "fact_not_in_quote"


@pytest.mark.parametrize("change,reason,correct,authentic", [
    ("missing_key", "fact_keys_mismatch", 1, 2),
    ("extra_key", "fact_keys_mismatch", 2, 2),
    ("wrong_value", "fact_values_mismatch", 1, 2),
    ("parentheses", "fact_values_mismatch", 1, 2),
    ("wrong_binding", "citation_binding_mismatch", 2, 2),
    ("fabricated_quote", "inauthentic_citation", 2, 1),
    ("wrong_lines", "inauthentic_citation", 2, 1),
    ("missing_citation", "uncited_fact", 2, 1),
    ("duplicate_citation", "citation_binding_mismatch", 2, 3),
    ("unreviewed_source", "citation_binding_mismatch", 2, 2),
])
def test_failures_separate_contract_facts_authenticity_and_binding(
    change, reason, correct, authentic,
):
    case = load_agent_dataset("v2").cases[0]
    answer = reviewed_answer(case)
    if change == "missing_key":
        del answer["facts"]["storage_method"]
    elif change == "extra_key":
        answer["facts"]["requested_key"] = "record_delivery"
    elif change == "wrong_value":
        answer["facts"]["storage_method"] = "append"
    elif change == "parentheses":
        answer["facts"]["storage_method"] = "record_delivery()"
    elif change == "wrong_binding":
        answer["citations"][0]["fact"] = "requested_key"
    elif change == "fabricated_quote":
        answer["citations"][0]["quote"] += " made up"
    elif change == "wrong_lines":
        answer["citations"][0].update(start_line=6, end_line=6)
    elif change == "missing_citation":
        answer["citations"].pop()
    elif change == "duplicate_citation":
        answer["citations"].append(copy.deepcopy(answer["citations"][0]))
    elif change == "unreviewed_source":
        answer["citations"][0].update(path="billing/outbox.py", start_line=1, end_line=1,
                                       quote="def dispatch_receipts(outbox, transport):")
    grade = grade_investigation(json.dumps(answer), case, case.documents)
    assert not grade["passed"] and grade["schema_valid"]
    assert reason in grade["failure_reasons"]
    assert grade["correct_fact_count"] == correct
    assert grade["authentic_citations"] == authentic


def test_missing_evidence_is_validly_reported_as_an_incomplete_answer():
    case = load_agent_dataset("v2").cases[0]
    text = '{"facts":{"deduplication_key":"","storage_method":""},"citations":[]}'
    grade = grade_investigation(text, case, case.documents)
    assert grade["schema_valid"] and grade["field_keys_correct"] and not grade["passed"]
    assert grade["correct_fact_count"] == grade["valid_citations"] == 0
    assert grade["failure_reasons"] == ["fact_values_mismatch", "uncited_fact"]


@pytest.mark.parametrize("text", [
    '{"facts":{},"facts":{},"citations":[]}',
    '{"facts":{},"citations":[],"note":"extra"}',
    '{"facts":{"storage_method":NaN},"citations":[]}',
    '{"facts":{"storage_method":42},"citations":[]}',
    '```json\n{"facts":{},"citations":[]}\n```',
])
def test_invalid_schema_is_not_misreported_as_semantic_or_retrieval_failure(text):
    case = load_agent_dataset("v2").cases[0]
    grade = grade_investigation(text, case, case.documents)
    assert not grade["schema_valid"] and not grade["format_valid"] and not grade["passed"]
    assert grade["failure_reasons"] == ["invalid_answer_schema"]
    assert grade["expected_fact_count"] == 2


@pytest.mark.parametrize("identity", ["retry-current-checkout", "export-copy-profile"])
def test_stale_and_foreign_quotes_are_separate_from_correct_current_facts(identity):
    case = next(case for case in load_agent_dataset("v2").cases if case.id == identity)
    answer = reviewed_answer(case)
    old = case.foreign_documents or case.documents
    for citation in answer["citations"]:
        citation["quote"] = "\n".join(old[citation["path"]].splitlines()[
            citation["start_line"] - 1:citation["end_line"]])
    grade = grade_investigation(json.dumps(answer), case, {**case.documents, **case.after_index})
    assert grade["facts_correct"] and grade["correct_fact_count"] == 2
    assert not grade["passed"] and grade["authentic_citations"] == 0
    assert grade["stale_or_foreign_quotes"] == 2


@pytest.mark.parametrize("change", ["project", "family", "value_kind", "alternative"])
def test_dataset_rejects_split_leaks_and_unreviewed_contracts(change):
    raw = load_agent_dataset("v2").model_dump()
    if change in {"project", "family"}:
        raw["cases"][4][change] = raw["cases"][0][change]
    elif change == "value_kind":
        del raw["cases"][0]["value_kinds"]["storage_method"]
    else:
        raw["cases"][0]["alternative_evidence"]["storage_method"][0]["path"] = "missing.py"
    with pytest.raises(ValidationError):
        InvestigationDatasetV2.model_validate(raw)


def test_default_v1_prompt_and_grade_keep_their_original_contract():
    case = load_agent_dataset().cases[0]
    assert not isinstance(case, InvestigationCaseV2)
    assert '"requested_key"' in investigation_prompt(case)
    assert "project" not in case.model_dump() and "value_kinds" not in case.model_dump()
    assert grade_investigation("{}", case, case.documents) == {
        "passed": False, "format_valid": False, "facts_correct": False,
        "valid_citations": 0, "total_citations": 0, "stale_or_foreign_quotes": 0,
        "reason": "invalid_answer_schema",
    }
