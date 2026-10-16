"""Immutable experiment snapshots derived from each isolated Runtime Event Log."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventide.budget_prices import CostLedger
from eventide.config import Settings
from eventide.normalization import normalize
from eventide.store import RuntimeStore
from eventide.usage import build_usage_ledger

if TYPE_CHECKING:
    from eventide.evaluation_calls import EvaluationCallBridge


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def atomic_json(path: Path, value: Any) -> dict[str, str]:
    body = json_bytes(value)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    return {"path": str(path), "sha256": hashlib.sha256(body).hexdigest()}


def runtime_revision() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "eventide").rglob("*.py")) if path.is_file()
    }
    result: dict[str, Any] = {
        "commit": None, "dirty": None, "python": platform.python_version(),
        "code_sha256": hashlib.sha256(json_bytes(files)).hexdigest(),
    }
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True, check=True, timeout=10,
        )
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, check=True, timeout=10,
        )
        result.update(commit=commit.stdout.decode().strip(), dirty=bool(dirty.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def safe_configuration(settings: Settings, *, live: bool) -> dict[str, Any]:
    return {
        "provider": settings.provider, "model": settings.model,
        "adapter": "configured_provider" if live else "scripted",
        "endpoint_sha256": hashlib.sha256(settings.base_url.encode()).hexdigest()
        if settings.base_url else None,
        "max_output_tokens": settings.max_tokens, "max_steps": settings.max_steps,
        "task_max_steps": settings.task_max_steps, "context_limit": settings.context_limit,
        "observed_token_limit": settings.run_token_limit,
        "model_timeout": settings.model_timeout, "command_timeout": settings.command_timeout,
        "workers_enabled": settings.workers_enabled, "external_mcp_enabled": False,
        "embedding": {"provider": settings.embedding_provider, "model": settings.embedding_model,
                      "dimensions": settings.embedding_dim},
    }


def collect_events(store: RuntimeStore) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for workspace in store.list_workspaces():
        for session in store.list_sessions(workspace.workspace_id):
            events.extend({**event, "workspace_id": workspace.workspace_id}
                          for event in store.session_events(session["id"]) if not event["partial"])
    # Canonical sequence is per Session. No wall-clock ordering is used to deduplicate attempts.
    return sorted(events, key=lambda event: (event["session_id"], event["session_seq"]))


def case_evidence(
    store: RuntimeStore, directory: Path, *, main_run_id: str | None,
    model_authority: EvaluationCallBridge | None = None,
) -> dict[str, Any]:
    events = collect_events(store)
    usage = build_usage_ledger(events)
    authority = model_authority.evidence(directory) if model_authority is not None else None
    references = list(usage.get("authority_references", []))
    references.extend({
        "run_id": event["payload"].get("authority_run_id"),
        "case_id": event["payload"].get("case_id"),
        "case_index": event["payload"].get("case_index"),
    } for event in events if event["type"] == "evaluation.model_request")
    if references and authority is None:
        raise ValueError("External model usage requires its authoritative evidence")
    if authority is not None:
        for reference in references:
            if any(reference[key] != authority[field] for key, field in (
                ("run_id", "authority_run_id"), ("case_id", "case_id"),
                ("case_index", "case_index"),
            )):
                raise ValueError("External model authority does not match execution evidence")
        usage = authority["usage"]
        if main_run_id is None:
            main_run_id = next((e["run_id"] for e in events
                                if e["type"] == "evaluation.model_request"), None)
    event_file = atomic_json(directory / "events.json", events)
    runs: dict[str, dict[str, Any]] = {}
    for event in events:
        run_id = event.get("run_id")
        if not run_id:
            continue
        run = runs.setdefault(run_id, {
            "run_id": run_id, "session_id": event["session_id"],
            "workspace_id": event["workspace_id"],
        })
        payload, kind = event["payload"], event["type"]
        if kind in {"run.admitted", "run.started"}:
            for key in ("project_id", "mode", "model", "generation", "execution_config",
                        "execution", "parent_run_id", "continuation_of", "source_checkpoint"):
                if key in payload:
                    run[key] = payload[key]
        elif kind == "context.configured":
            run["context_fingerprints"] = payload
        elif kind == "skills.snapshot":
            run["skill_snapshot"] = payload
        elif kind in {"run.completed", "run.failed", "run.interrupted"}:
            run["status"] = kind.removeprefix("run.")
        elif kind == "skill.loaded":
            run.setdefault("loaded_skills", []).append(payload)
    for run in runs.values():
        identity = store.get_run_identity(run["run_id"])
        if identity:
            run["turn_id"] = identity["turn_id"]
            run["continuation_of"] = identity.get("continuation_of")
    if main_run_id is None:
        main_run_id = next((call["run_id"] for call in usage["calls"]
                            if call["kind"] == "agent" and not runs.get(
                                call["run_id"], {},
                            ).get("parent_run_id")), None)
    attempts = []
    for event in events:
        if event["type"] == "model.attempt_started":
            attempts.append({"run_id": event["run_id"], **{
                key: event["payload"].get(key) for key in (
                    "attempt_id", "request_id", "attempt", "provider", "model", "kind",
                    "generation", "max_output_tokens",
                )
            }})
    tool_results = [event for event in events if event["type"] == "tool.completed"]
    errors = [event for event in tool_results if event["payload"].get("is_error")]
    blocks = sum("Permission denied" in str(event["payload"].get("content", ""))
                 for event in errors)
    return {
        "tool_metrics": {"calls": len(tool_results), "errors": len(errors) - blocks,
                         "safety_blocks": blocks},
        "source": "eventide.execution_and_model_authorities" if authority else "eventide.event_log",
        "main_run_id": main_run_id,
        "database": str(store.path), "events": event_file,
        "runs": list(runs.values()),
        "model_attempts": authority["model_attempts"] if authority else attempts, "usage": usage,
        "costs": authority["costs"] if authority else CostLedger(store).snapshot(),
        **({"model_authority": authority} if authority else {}),
        "price_versions": authority["price_versions"] if authority else [
            event["payload"] for event in events if event["type"] == "price.registered"
        ],
    }


def safe_failure(exc: BaseException, stage: str, settings: Settings) -> dict[str, str]:
    secrets = tuple(value for value in (settings.api_key, settings.embedding_api_key,
                                       settings.base_url, settings.embedding_base_url) if value)
    return {"type": type(exc).__name__, "stage": stage,
            "message": str(normalize(str(exc), secrets))[:2000]}


def summarize_usage(results: list[Any]) -> dict[str, Any]:
    ledgers = [item.evidence["usage"] for item in results if "usage" in item.evidence]
    buckets: dict[str, dict[str, int]] = {"reported_totals": {}, "estimated_totals": {}}
    for ledger in ledgers:
        for name, bucket in buckets.items():
            for field, number in ledger[name].items():
                bucket[field] = bucket.get(field, 0) + number
    return {
        **buckets, "attempt_count": sum(item["attempt_count"] for item in ledgers),
        "unknown_attempts": sum(item["unknown_attempts"] for item in ledgers),
        "incomplete_attempts": sum(item["incomplete_attempts"] for item in ledgers),
        "complete": bool(ledgers) and len(ledgers) == len(results)
        and all(item["complete"] for item in ledgers),
        "missing_evidence_cases": [item.id for item in results if "usage" not in item.evidence],
        "source": ("exclusive_model_authorities" if any(
            "model_authority" in item.evidence for item in results
        ) else "exclusive_case_event_logs"), "covered_cases": len(ledgers),
    }
