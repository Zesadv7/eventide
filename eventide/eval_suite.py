"""Versioned benchmark definitions and frozen, independently graded task inputs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["understanding", "diagnosis", "single_file", "cross_file", "recovery_delivery"]
Split = Literal["development", "holdout"]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or "\\" in value
            or str(path) != value or ".git" in path.parts or ".eventide" in path.parts):
        raise ValueError("Benchmark paths must be canonical relative files")
    return value


class Fact(StrictModel):
    id: Identifier
    answer: Annotated[str, Field(min_length=1, max_length=500)]
    path: str
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]


class FactGrade(StrictModel):
    kind: Literal["facts"]
    facts: Annotated[list[Fact], Field(min_length=1, max_length=8)]


class AcceptanceGrade(StrictModel):
    kind: Literal["acceptance"]
    allowed_changes: Annotated[list[str], Field(min_length=1, max_length=8)]


class RecoveryGrade(StrictModel):
    kind: Literal["recovery_delivery"]
    scenario: Literal["restart_then_adopt", "stale_acceptance_refused"]
    allowed_changes: Annotated[list[str], Field(min_length=1, max_length=8)]


Grade = Annotated[FactGrade | AcceptanceGrade | RecoveryGrade, Field(discriminator="kind")]


class BenchmarkTask(StrictModel):
    id: Identifier
    version: Annotated[int, Field(ge=1)]
    family: Identifier
    category: Category
    split: Split
    prompt: Annotated[str, Field(min_length=1, max_length=20000)]
    workspace: dict[str, str]
    script: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=36)]
    grade: Grade

    @model_validator(mode="after")
    def validate_task(self) -> BenchmarkTask:
        if not self.workspace or len(self.workspace) > 100:
            raise ValueError("Benchmark needs 1-100 fixture files")
        if sum(len(value.encode()) for value in self.workspace.values()) > 2 * 1024 * 1024:
            raise ValueError("Benchmark fixture exceeds 2 MiB")
        for path in self.workspace:
            relative_file(path)
        if isinstance(self.grade, FactGrade):
            if self.category not in {"understanding", "diagnosis"}:
                raise ValueError("Fact grading is only for understanding/diagnosis")
            if len({fact.id for fact in self.grade.facts}) != len(self.grade.facts):
                raise ValueError("Duplicate annotated fact")
            for fact in self.grade.facts:
                relative_file(fact.path)
                lines = self.workspace.get(fact.path, "").splitlines()
                if not 1 <= fact.start_line <= fact.end_line <= len(lines):
                    raise ValueError("Annotated fact must cite existing fixture lines")
        else:
            expected = ("recovery_delivery" if isinstance(self.grade, RecoveryGrade)
                        else self.category)
            if expected not in {"single_file", "cross_file", "recovery_delivery"}:
                raise ValueError("Repair grading is only for repair/lifecycle tasks")
            if isinstance(self.grade, RecoveryGrade) and self.category != "recovery_delivery":
                raise ValueError("Lifecycle grading requires recovery_delivery category")
            if self.category == "recovery_delivery" and not isinstance(self.grade, RecoveryGrade):
                raise ValueError("Recovery task requires a lifecycle scenario")
            allowed = self.grade.allowed_changes
            if len(set(allowed)) != len(allowed):
                raise ValueError("Duplicate allowed change")
            for path in allowed:
                relative_file(path)
                if path not in self.workspace or path.startswith("tests/") or path == "README.md":
                    raise ValueError("Allowed changes must be existing business source files")
            if self.category == "single_file" and len(allowed) != 1:
                raise ValueError("Single-file task must allow exactly one source file")
            if self.category == "cross_file" and len(allowed) < 2:
                raise ValueError("Cross-file task must require multiple source files")
            if not any(path.startswith("tests/test_") for path in self.workspace):
                raise ValueError("Repair tasks require fixed unittest files")
        return self


class TaskReference(StrictModel):
    id: Identifier
    path: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BenchmarkManifest(StrictModel):
    schema_version: Literal[1]
    id: Identifier
    version: Annotated[int, Field(ge=1)]
    description: str
    tasks: Annotated[list[TaskReference], Field(min_length=20, max_length=20)]


def _read_file(root: Path, name: str) -> bytes:
    relative_file(name)
    path = root / name
    if path.resolve() != path or not path.is_file() or path.stat().st_size > 3 * 1024 * 1024:
        raise ValueError("Benchmark reference missing, linked, or too large")
    return path.read_bytes()


def load_benchmark(path: Path, data: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = BenchmarkManifest.model_validate(data)
    root = path.resolve().parent
    tasks: list[BenchmarkTask] = []
    fingerprints: list[str] = []
    for reference in manifest.tasks:
        body = _read_file(root, reference.path)
        fingerprint = hashlib.sha256(body).hexdigest()
        if fingerprint != reference.sha256:
            raise ValueError(
                f"Frozen task changed: {reference.id}; update its version and manifest"
            )
        task = BenchmarkTask.model_validate(json.loads(body))
        if task.id != reference.id:
            raise ValueError("Task reference identity mismatch")
        tasks.append(task)
        fingerprints.append(fingerprint)
    if len({task.id for task in tasks}) != 20:
        raise ValueError("Benchmark task IDs must be unique")
    expected = {"understanding": (2, 2), "diagnosis": (2, 2), "single_file": (4, 2),
                "cross_file": (3, 1), "recovery_delivery": (1, 1)}
    counts: Counter[tuple[str, str]] = Counter((task.category, task.split) for task in tasks)
    if any(counts[(category, split)] != count for category, pair in expected.items()
           for split, count in zip(("development", "holdout"), pair, strict=True)):
        raise ValueError("Benchmark requires 12 development/8 holdout tasks with fixed categories")
    families: dict[str, str] = {}
    for task in tasks:
        prior = families.setdefault(task.family, task.split)
        if prior != task.split:
            raise ValueError("A task family cannot cross development and holdout splits")
    suite_digest = hashlib.sha256(json.dumps(
        manifest.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return [
        {
            "id": task.id, "prompt": task.prompt, "workspace": task.workspace,
            "script": task.script, "expected": {"status": "completed"},
            "acceptance_tests": isinstance(task.grade, AcceptanceGrade),
            "grade": task.grade.model_dump(),
            "benchmark": {
                "suite_id": manifest.id, "suite_version": manifest.version,
                "suite_sha256": suite_digest, "task_version": task.version,
                "task_sha256": fingerprint, "category": task.category,
                "split": task.split, "family": task.family,
            },
        }
        for task, fingerprint in zip(tasks, fingerprints, strict=True)
    ]


def grade_facts(case: dict[str, Any], output: str) -> tuple[str, ...]:
    """Compare annotated facts and exact source references, never ask the model to grade itself."""
    try:
        answer = json.loads(output)
        if not isinstance(answer, dict) or set(answer) != {"facts"}:
            raise ValueError("Expected facts object")
        claims = answer["facts"]
        if not isinstance(claims, list):
            raise ValueError("Expected facts list")
        actual = [Fact.model_validate(item) for item in claims]
        expected = [Fact.model_validate(item) for item in case["grade"]["facts"]]
        if len(actual) != len(expected) or len({item.id for item in actual}) != len(actual):
            raise ValueError("Missing or duplicate fact")
        indexed = {item.id: item for item in actual}
        return tuple(f"incorrect fact or citation: {fact.id}" for fact in expected
                     if indexed.get(fact.id) != fact)
    except (ValueError, TypeError, KeyError):
        return ("invalid structured facts or citations",)
