"""Frozen code-investigation tasks; source facts, never model self-grading, decide success."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventide.rag.answer_evaluation import Citation

if TYPE_CHECKING:
    from eventide.host import RuntimeHost
    from eventide.store import WorkspaceRecord

CANDIDATE_GUIDANCE = (
    "For known paths use glob/read_file; for identifiers or error text use search_text. "
    "For a concept whose code vocabulary is unclear, search_knowledge can locate candidates. "
    "Check current source before relying on a citation. Stale or unavailable index evidence "
    "requires reading/searching current files. Use whichever investigation path supplies "
    "enough evidence; semantic search is optional."
)

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
                "valid_citations": 0, "total_citations": 0, "stale_or_foreign_quotes": 0,
                "reason": "invalid_answer_schema"}
    expected = {fact.key: fact for fact in case.facts}
    cited: set[str] = set()
    seen: set[tuple[str, str, int, int]] = set()
    valid = 0
    old_quotes = 0
    old_sources = case.foreign_documents or (case.documents if case.after_index else {})
    for citation in answer.citations:
        lines = documents.get(citation.path, "").splitlines()
        old_lines = old_sources.get(citation.path, "").splitlines()
        current_quote = "\n".join(lines[citation.start_line - 1:citation.end_line])
        if (1 <= citation.start_line <= citation.end_line <= len(old_lines)
                and citation.quote != current_quote
                and citation.quote == "\n".join(old_lines[
                    citation.start_line - 1:citation.end_line])):
            old_quotes += 1
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
            "total_citations": len(answer.citations), "stale_or_foreign_quotes": old_quotes,
            "reason": "frozen_current_source_grade"}


def agent_comparison_report(host: RuntimeHost, control_runs: list[str]) -> dict[str, Any]:
    """Reconstruct attempted and unattempted slots without assuming every Run produced a row."""
    from eventide.rag.upgrade_evaluation import experiment_evidence, percentile

    events = [e for run in control_runs for e in host.store.run_events(run)]
    accepted = [e["payload"]["configuration"] for e in events
                if e["type"] == "control.operation_accepted"]
    plan = accepted[0]["plan"]
    rows = [e["payload"] for e in events if e["type"] == "retrieval.agent_result"]
    for row in rows:
        agent_events = host.store.run_events(row["agent_run_id"])
        row["tool_calls"] = [e["payload"] for e in agent_events if e["type"] == "tool.completed"]
        row["context"] = [e["payload"] for e in agent_events
                          if e["type"] in {"context.configured", "skills.snapshot"}]
        row["retrieval_status"] = [e["payload"] for e in agent_events
                                   if e["type"] == "retrieval.status"]
        retrievals = [call["metadata"]["retrieval"] for call in row["tool_calls"]
                      if "retrieval" in call.get("metadata", {})]
        row["cross_workspace_results"] = sum(
            result["workspace_id"] != row["workspace_id"] for result in retrievals
        )
        row["stale_hits_marked_fresh"] = sum(
            hit["freshness"] == "fresh" and hit["indexed_content_hash"] !=
            row["source_hashes"].get(hit["file"])
            for result in retrievals for hit in result["hits"]
        )
    identity = accepted[0]["experiment"]
    started = {(a["attempt"]["case_id"], a["attempt"]["repetition"], a["attempt"]["arm"])
               for a in accepted}
    recorded = {(r["case_id"], r["repetition"], r["arm"]) for r in rows}
    all_runs = control_runs + [a["agent_run_id"] for a in accepted
                              if host.store.get_run_identity(a["agent_run_id"]) is not None]
    arms = {}
    for arm in ("baseline", "candidate"):
        selected = [row for row in rows if row["arm"] == arm]
        valid = sum(row.get("grade", {}).get("valid_citations", 0) for row in selected)
        total = sum(row.get("grade", {}).get("total_citations", 0) for row in selected)
        elapsed = [row["duration_ms"] for row in selected]
        arms[arm] = {
            "expected": sum(slot["arm"] == arm for slot in plan),
            "attempted": sum(key[2] == arm for key in started),
            "passed": sum(row["status"] == "completed" and row["grade"]["passed"]
                          for row in selected),
            "failed": sum(row["status"] != "completed" or not row["grade"]["passed"]
                          for row in selected),
            "valid_citations": valid, "total_citations": total,
            "valid_citation_ratio": valid / total if total else None,
            "stale_or_foreign_quotes": sum(row.get("grade", {}).get(
                "stale_or_foreign_quotes", 0) for row in selected),
            "cross_workspace_results": sum(row["cross_workspace_results"] for row in selected),
            "stale_hits_marked_fresh": sum(row["stale_hits_marked_fresh"] for row in selected),
            "latency_p50_ms": percentile(elapsed, .5),
            "latency_p95_ms": percentile(elapsed, .95),
            "results": selected,
        }
    terminal = all(host.store.run_is_terminal(run) for run in control_runs)
    return {
        "schema_version": 1, **identity, "arms": arms,
        "status": "completed" if len(rows) == len(plan) and terminal else
                  "aborted" if terminal else "running",
        "remaining": [slot for slot in plan if (
            slot["case_id"], slot["repetition"], slot["arm"],
        ) not in started],
        "missing_results": [list(key) for key in sorted(started - recorded)],
        "evidence": experiment_evidence(host, all_runs),
        "limitations": [
            "12 model steps and 20000 observed Tokens apply to each Agent Run",
            "Preliminary index preparation is additional usage, included in total ledger",
            "300 second deadline includes index preparation and Agent execution",
            "Unknown usage aborts the batch; no failure is selectively retried",
            "Exact facts and source quotations grade code investigation, not repair quality",
        ],
    }


async def run_agent_comparison(
    host: RuntimeHost, *, output_directory: Path, split: Split = "development",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Run frozen file-tool/semantic pairs through the real Host, with no alternate agent."""
    import asyncio
    import time
    import uuid

    from eventide.eval_evidence import atomic_json, runtime_revision
    from eventide.models import RunRequest
    from eventide.rag.upgrade_evaluation import experiment_events, write_corpus
    from eventide.retrieval_runtime import managed_index_dir, prepare_index
    from eventide.usage import build_usage_ledger
    from eventide.utils import owned_local_work
    from eventide.workspace import git

    if confirmed is not True:
        raise ValueError("Agent retrieval experiment requires explicit confirmation")
    if (host._enable_mcp or host.settings.workers_enabled
            or host.settings.max_steps != 12 or host.settings.run_token_limit != 20_000
            or host.settings.retrieval_query_rewrite
            or host.settings.reranker_model_path is not None
            or host.settings.embedding_provider != "openai_compatible"):
        raise ValueError("Use an isolated Host with MCP/workers/rewrite/rerank disabled, "
                         "12 steps, 20000 observed Tokens and remote embeddings")
    await host.initialize()
    dataset = load_agent_dataset()
    plan = attempt_plan(dataset, split)
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=False)
    configuration = host._run_intent(None)
    experiment = {
        "experiment_id": f"agent_retrieval_{uuid.uuid4().hex}",
        "dataset_sha256": DATASET_SHA256, "version": dataset.version, "split": split,
        "runtime": runtime_revision(), "model_configuration": configuration,
        "candidate_guidance": CANDIDATE_GUIDANCE, "skills": [], "mcp": False,
        "limits": {"model_steps": 12, "agent_observed_tokens": 20_000, "seconds": 300},
    }
    controls: list[str] = []
    by_id = {case.id: case for case in dataset.cases}
    for index, slot in enumerate(plan):
        if host._run_intent(None) != configuration:
            raise ValueError("Chat model configuration changed during frozen comparison")
        case = by_id[slot["case_id"]]
        directory = output_directory / f"{index + 1:02d}-{case.id}-{slot['arm']}"
        source = directory / "source"
        await owned_local_work(write_corpus, source, case.foreign_documents or case.documents)
        source_workspace = host.resolve_or_register_workspace(source)
        workspace = source_workspace
        if case.foreign_documents:
            await owned_local_work(git, source, "add", ".")
            await owned_local_work(git, source, "-c", "user.name=Eventide evaluation",
                                   "-c", "user.email=evaluation@invalid", "commit", "-m", "fixture")
            actual = directory / "checkout"
            await owned_local_work(git, source, "worktree", "add", "--detach", str(actual))
            for name, body in case.documents.items():
                (actual / name).write_text(body, encoding="utf-8")
            workspace = host.resolve_or_register_workspace(actual)
        root = Path(workspace.path)
        session = host.create_session(workspace_id=workspace.workspace_id)
        agent_run = f"investigate_{uuid.uuid4().hex}"
        control_run = f"investigation_{uuid.uuid4().hex}"
        accepted = {"experiment": experiment, "plan": plan, "attempt": slot,
                    "case": case.model_dump(), "agent_run_id": agent_run,
                    "workspace_id": workspace.workspace_id}

        async def execute(
            owned: str, slot: dict[str, Any] = slot, case: InvestigationCase = case,
            agent_run: str = agent_run, workspace: WorkspaceRecord = workspace,
            source_workspace: WorkspaceRecord = source_workspace, root: Path = root,
            session: str = session,
        ) -> None:
            started = time.perf_counter()
            row: dict[str, Any] = {**slot, "status": "running", "agent_run_id": agent_run,
                                   "workspace_id": workspace.workspace_id}
            try:
                if slot["arm"] == "candidate":
                    if case.foreign_documents:
                        await prepare_index(host, owned, source_workspace, settings=host.settings)
                    await prepare_index(host, owned, workspace, settings=host.settings)
                    if not build_usage_ledger(host.model_usage_events(owned))["complete"]:
                        raise RuntimeError("Index preparation usage unknown; batch stopped")
                row["preparation_duration_ms"] = (time.perf_counter() - started) * 1000
                row["preparation_usage"] = build_usage_ledger(host.model_usage_events(owned))
                for name, body in case.after_index.items():
                    (root / name).write_text(body, encoding="utf-8")
                documents = {name: (root / name).read_text(encoding="utf-8")
                             for name in case.documents}
                row["source_hashes"] = {name: hashlib.sha256(body.encode()).hexdigest()
                                        for name, body in documents.items()}
                prompt = investigation_prompt(case)
                if slot["arm"] == "candidate":
                    prompt += "\n" + CANDIDATE_GUIDANCE
                result = await host.run(RunRequest(
                    prompt, session, run_id=agent_run, mode="plan", selected_skills=[],
                    generation=configuration["generation"], knowledge_index="existing",
                ))
                current = {name: (root / name).read_text(encoding="utf-8")
                           for name in case.documents}
                grade = grade_investigation(result.output, case, current)
                unchanged = documents == current
                grade["source_unchanged"] = unchanged
                grade["passed"] = grade["passed"] and unchanged
                agent_events = host.store.run_events(agent_run)
                tools = [e["payload"] for e in agent_events if e["type"] == "tool.completed"]
                row.update(status=result.status, grade=grade,
                           tool_call_count=len(tools), steps=result.steps,
                           index_directory=str(managed_index_dir(host, workspace)))
                if not build_usage_ledger(experiment_events(host, [owned, agent_run]))["complete"]:
                    row["status"] = "usage_unknown"
                    raise RuntimeError("Model usage unknown; batch stopped")
                if result.status == "failed":
                    raise RuntimeError("Agent execution failed; batch stopped")
            except BaseException as exc:
                if row["status"] == "running":
                    row["status"] = (
                        "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
                    )
                row["error_type"] = type(exc).__name__
                raise
            finally:
                row["duration_ms"] = (time.perf_counter() - started) * 1000
                host.store.append_event(owned, "retrieval.agent_result", row)

        async def bounded_execute(
            owned: str, execute: Callable[[str], Awaitable[None]] = execute,
        ) -> None:
            await asyncio.wait_for(execute(owned), timeout=300)

        try:
            controls.append(control_run)
            await host.run_control_operation(
                kind="retrieval_benchmark", project_id=workspace.project_id,
                run_id=control_run, accepted=accepted, execute=bounded_execute,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The canonical control terminal preserves the failure; stop, never replace this slot.
            if host.store.get_run_identity(control_run) is None:
                controls.remove(control_run)
                raise
            return agent_comparison_report(host, controls)
        finally:
            if controls and host.store.get_run_identity(controls[0]) is not None:
                atomic_json(output_directory / "report.json",
                            agent_comparison_report(host, controls))
    return agent_comparison_report(host, controls)
