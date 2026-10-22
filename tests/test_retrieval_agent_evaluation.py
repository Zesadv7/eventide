"""The Agent investigation contract is frozen before any real-model comparison."""

import json

import pytest
from pydantic import ValidationError

from eventide.rag.agent_evaluation import (
    InvestigationDataset,
    attempt_plan,
    grade_investigation,
    investigation_prompt,
    load_agent_dataset,
)


def exact_answer(case):
    sources = {**case.documents, **case.after_index}
    return {
        "facts": {fact.key: fact.value for fact in case.facts},
        "citations": [
            {**fact.evidence.model_dump(), "fact": fact.key,
             "quote": "\n".join(sources[fact.evidence.path].splitlines()[
                 fact.evidence.start_line - 1:fact.evidence.end_line])}
            for fact in case.facts
        ],
    }


def test_frozen_tasks_and_attempts_have_exact_nonselective_pairing():
    dataset = load_agent_dataset()
    development = attempt_plan(dataset, "development")
    holdout = attempt_plan(dataset, "holdout")
    assert len(development) == 8 and len(holdout) == 16
    for rows in (development, holdout):
        assert [r["arm"] for r in rows[:4]] == [
            "baseline", "candidate", "candidate", "baseline",
        ]
        for first, second in zip(rows[::2], rows[1::2], strict=True):
            assert (first["case_id"], first["repetition"]) == (
                second["case_id"], second["repetition"],
            )
            assert first["arm"] != second["arm"]
    assert sum(bool(c.after_index) for c in dataset.cases) == 1
    assert sum(bool(c.foreign_documents) for c in dataset.cases) == 1
    with pytest.raises(ValueError, match="explicitly"):
        attempt_plan(dataset, "all")


@pytest.mark.parametrize("case", load_agent_dataset().cases, ids=lambda c: c.id)
def test_facts_require_current_source_quotes_without_requiring_any_tool(case):
    sources = {**case.documents, **case.after_index}
    answer = exact_answer(case)
    grade = grade_investigation(json.dumps(answer), case, sources)
    assert grade["passed"]
    assert grade["valid_citations"] == grade["total_citations"] == len(case.facts)
    prompt = investigation_prompt(case)
    assert "documents" not in prompt and "after_index" not in prompt
    assert "search_knowledge" not in prompt
    answer["citations"][0]["quote"] += " fabricated"
    assert not grade_investigation(json.dumps(answer), case, sources)["passed"]


@pytest.mark.parametrize("change", ["fact", "citation", "path", "missing", "duplicate",
                                    "duplicate_json_key", "nonfinite", "fence"])
def test_grader_rejects_incorrect_or_invalid_claims(change):
    case = load_agent_dataset().cases[0]
    answer = exact_answer(case)
    if change == "fact":
        answer["facts"][case.facts[0].key] = "incorrect"
    elif change == "citation":
        answer["citations"][0]["start_line"] = 0
    elif change == "path":
        answer["citations"][0]["path"] = "../receipt_store.py"
    elif change == "missing":
        answer["citations"].pop()
    elif change == "duplicate":
        answer["citations"].append(answer["citations"][0])
    text = json.dumps(answer)
    if change == "duplicate_json_key":
        text = '{"facts":{},' + text[1:]
    elif change == "nonfinite":
        text = text.replace('"start_line": 1', '"start_line": NaN')
    elif change == "fence":
        text = "```json\n" + text + "\n```"
    assert not grade_investigation(text, case, case.documents)["passed"]


def test_stale_or_foreign_citations_fail_against_current_source():
    for case in load_agent_dataset().cases:
        old = case.foreign_documents or (case.documents if case.after_index else None)
        if old is None:
            continue
        answer = exact_answer(case)
        for citation in answer["citations"]:
            citation["quote"] = "\n".join(old[citation["path"]].splitlines()[
                citation["start_line"] - 1:citation["end_line"]])
        grade = grade_investigation(json.dumps(answer), case,
                                    {**case.documents, **case.after_index})
        assert grade["facts_correct"] and not grade["passed"]


@pytest.mark.parametrize("change", ["path", "fact", "split", "duplicate"])
def test_frozen_manifest_validates_source_boundaries_and_balance(change):
    raw = load_agent_dataset().model_dump()
    if change == "path":
        raw["cases"][0]["documents"]["../outside.py"] = "escape"
    elif change == "fact":
        raw["cases"][0]["facts"][0]["value"] = "invented"
    elif change == "split":
        raw["cases"][0]["split"] = "holdout"
    elif change == "duplicate":
        raw["cases"][1]["id"] = raw["cases"][0]["id"]
    with pytest.raises(ValidationError):
        InvestigationDataset.model_validate(raw)
