"""Frozen code-investigation tasks; source facts, never model self-grading, decide success."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventide.rag.answer_evaluation import Citation

DATASET_SHA256 = "26a69ccf2e590f0ad41aeb86cf3411d476c8485031056fbb12b1d73747084ce4"
Category = Literal["identifier", "concept", "cross_file", "freshness"]
Split = Literal["development", "holdout"]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str
    value: str
    evidence: Citation


class InvestigationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    split: Split
    category: Category
    question: str
    documents: dict[str, str]
    after_index: dict[str, str] = Field(default_factory=dict)
    foreign_documents: dict[str, str] = Field(default_factory=dict)
    facts: list[Fact] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sources(self) -> InvestigationCase:
        for source in (self.documents, self.after_index, self.foreign_documents):
            for name, body in source.items():
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                        or path.as_posix() != name or any(p.startswith(".") for p in path.parts)
                        or not body):
                    raise ValueError("Invalid frozen source path or body")
        if not self.documents or not self.question.strip() or not self.id:
            raise ValueError("Task must contain source, identity and a question")
        if not self.after_index.keys() <= self.documents.keys():
            raise ValueError("Stale fixture must replace an existing source")
        if len({fact.key for fact in self.facts}) != len(self.facts):
            raise ValueError("Duplicate fact key")
        current = {**self.documents, **self.after_index}
        for fact in self.facts:
            evidence = fact.evidence
            lines = current.get(evidence.path, "").splitlines()
            if (not fact.key or not fact.value or not
                    1 <= evidence.start_line <= evidence.end_line <= len(lines)
                    or fact.value not in "\n".join(lines[
                        evidence.start_line - 1:evidence.end_line])):
                raise ValueError("Frozen fact lacks current source evidence")
        return self


class InvestigationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    version: Literal["retrieval-agent-v1"]
    cases: list[InvestigationCase]

    @model_validator(mode="after")
    def validate_splits(self) -> InvestigationDataset:
        if len(self.cases) != 8 or len({c.id for c in self.cases}) != 8:
            raise ValueError("Exactly eight uniquely identified tasks are required")
        if len({c.question for c in self.cases}) != 8:
            raise ValueError("Questions must be distinct")
        for split in ("development", "holdout"):
            if sorted(c.category for c in self.cases if c.split == split) != [
                "concept", "cross_file", "freshness", "identifier",
            ]:
                raise ValueError("Each split must contain all four categories once")
        return self


def load_agent_dataset() -> InvestigationDataset:
    path = Path(__file__).parents[1] / "bundled_evals/retrieval-agent-v1/dataset.json"
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != DATASET_SHA256:
        raise ValueError("Frozen Agent retrieval dataset changed")
    return InvestigationDataset.model_validate_json(body)


def attempt_plan(dataset: InvestigationDataset, split: Split) -> list[dict[str, Any]]:
    """One development pair and two holdout pairs, alternating first arm across pairs."""
    if split not in {"development", "holdout"}:
        raise ValueError("Choose development or holdout explicitly")
    rows: list[dict[str, Any]] = []
    for case in (c for c in dataset.cases if c.split == split):
        for repetition in range(1 if split == "development" else 2):
            arms = ("baseline", "candidate") if len(rows) // 2 % 2 == 0 else (
                "candidate", "baseline",
            )
            rows.extend({"case_id": case.id, "repetition": repetition, "arm": arm}
                        for arm in arms)
    return rows


class FactCitation(Citation):
    fact: str
    quote: str = Field(min_length=1, max_length=4000)


class InvestigationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    facts: dict[str, str]
    citations: list[FactCitation] = Field(min_length=1, max_length=10)


def investigation_prompt(case: InvestigationCase) -> str:
    """Only the question, requested field names and format reach the Agent."""
    return (
        case.question + "\nInvestigate the current checkout with the available read-only tools. "
        "Return only JSON, without markdown fences: "
        '{"facts":{"requested_key":"exact source identifier or literal"},'
        '"citations":[{"fact":"requested_key","path":"relative/file",'
        '"start_line":1,"end_line":2,"quote":"complete cited source lines"}]}. '
        "For every fact cite current source lines and copy their text exactly. "
        "Do not change files. Requested keys: "
        + ", ".join(fact.key for fact in case.facts)
    )


def grade_investigation(
    text: str, case: InvestigationCase, documents: dict[str, str],
) -> dict[str, Any]:
    """Check exact finite JSON, facts and independently read current source citations."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("Non-finite JSON constant")

    try:
        answer = InvestigationAnswer.model_validate(json.loads(
            text, object_pairs_hook=unique_object, parse_constant=invalid_constant,
        ))
    except ValueError:
        return {"passed": False, "format_valid": False, "facts_correct": False,
                "valid_citations": 0, "total_citations": 0, "reason": "invalid_answer_schema"}
    expected = {fact.key: fact for fact in case.facts}
    cited: set[str] = set()
    seen: set[tuple[str, str, int, int]] = set()
    valid = 0
    for citation in answer.citations:
        lines = documents.get(citation.path, "").splitlines()
        fact = expected.get(citation.fact)
        key = (citation.fact, citation.path, citation.start_line, citation.end_line)
        if (fact is not None and key not in seen
                and citation.path == fact.evidence.path
                and fact.evidence.start_line <= citation.start_line <= citation.end_line
                <= fact.evidence.end_line
                and 1 <= citation.start_line <= citation.end_line <= len(lines)
                and citation.quote == "\n".join(lines[
                    citation.start_line - 1:citation.end_line])
                and fact.value in citation.quote):
            valid += 1
            cited.add(citation.fact)
        seen.add(key)
    correct = answer.facts == {key: fact.value for key, fact in expected.items()}
    return {"passed": correct and cited == expected.keys() and valid == len(answer.citations),
            "format_valid": True, "facts_correct": correct, "valid_citations": valid,
            "total_citations": len(answer.citations), "reason": "frozen_current_source_grade"}
