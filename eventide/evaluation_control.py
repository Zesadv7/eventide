"""Scheduled offline mechanism checks; canonical control facts stay in the main Store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventide.config import Settings
from eventide.eval_evidence import atomic_json, runtime_revision
from eventide.eval_suite import Split
from eventide.evaluation_profiles import EvaluationPreset, EvaluationProfile, evaluation_profile
from eventide.evaluation_quote import evaluation_quote
from eventide.models import RunResult

if TYPE_CHECKING:
    from eventide.host import RuntimeHost
    from eventide.store import RuntimeStore


def is_evaluation_run(store: RuntimeStore, run_id: str) -> bool:
    """Recognize trusted control ownership, not a freely supplied source string."""
    identity = store.get_run_identity(run_id)
    if identity is None:
        return False
    intent: dict[str, Any] = next((e["payload"] for e in store.run_events(run_id)
                   if e["type"] == "run.admitted"), {})
    binding = intent.get("execution_binding", {})
    if intent.get("source") != "evaluation" or binding != {
        "kind": "evaluation", "evaluation_id": run_id,
        "workspace_id": intent.get("workspace_id"),
    }:
        return False
    session = store.get_session(identity["session_id"])
    if session is None or session["workspace_id"] != intent.get("workspace_id"):
        return False
    workspace = store.get_workspace(session["workspace_id"])
    if workspace.project_id != intent.get("project_id"):
        return False
    return any(e["type"] == "control.session" and e["payload"] == {
        "kind": "evaluation", "project_id": workspace.project_id,
    } for e in store.session_events(identity["session_id"]))


class EvaluationController:
    def __init__(self, host: RuntimeHost):
        self.host = host
        self.store = host.store

    def owner(self, project_id: str, *, create: bool = True) -> str:
        project = self.store.get_project(project_id)
        identity = f"evaluation_control_{project_id}"
        session = self.store.get_session(identity)
        if session is None and create:
            self.host.create_session(identity, workspace_id=project.source_workspace_id)
            self.store.append_fact(identity, "control.session", {
                "kind": "evaluation", "project_id": project_id,
            })
            self.store.update_session(identity, title="离线机制评测", archived=True)
        elif session is not None and (
            session["workspace_id"] != project.source_workspace_id
            or not any(e["type"] == "control.session" and e["payload"] == {
                "kind": "evaluation", "project_id": project_id,
            } for e in self.store.session_events(identity))
        ):
            raise ValueError("Evaluation control Session identity conflict")
        return identity

    async def admit(
        self, project_id: str, run_id: str, *, suite_id: str, suite_sha256: str,
        split: str = "development", request_fingerprint: str | None = None,
        mode: str = "offline", quote_hash: str | None = None, confirmed: bool = False,
        preset: EvaluationPreset = "none",
    ) -> str:
        from eventide.bundled_evaluations import load_builtin_suite
        from eventide.host import _Admission

        await self.host.initialize()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid evaluation identity")
        if split not in {"development", "holdout", "all"}:
            raise ValueError("Invalid evaluation split")
        if mode not in {"offline", "live"}:
            raise ValueError("Invalid evaluation mode")
        generation = None
        fact_response_format = None
        provider_config_hash = None
        quote = None
        profile = evaluation_profile(preset)
        if mode == "live":
            if confirmed is not True:
                raise ValueError("Live evaluation requires explicit confirmation")
            quote = evaluation_quote(self.host, project_id, suite_id=suite_id, split=split,
                                     preset=preset, profile=profile)
            if quote_hash != quote["quote_hash"]:
                raise ValueError("Evaluation estimate or model configuration changed; review again")
            generation = quote["generation"]
            fact_response_format = quote["fact_response_format"]
            provider_config_hash = self.host.provider_config_status()["config_hash"]
        selected: Split | None = ("development" if split == "development" else
                                  "holdout" if split == "holdout" else None)
        suite = load_builtin_suite(suite_id, split=selected)
        if suite["suite_sha256"] != suite_sha256:
            raise ValueError("Suite changed; refresh the catalog before starting")
        owner = self.owner(project_id)
        if self.host.session_busy(owner):
            raise ValueError("Project evaluation is busy")
        if self.store.get_run_identity(run_id):
            raise ValueError("Run identity already used")
        project = self.store.get_project(project_id)
        binding = {"kind": "evaluation", "evaluation_id": run_id,
                   "workspace_id": project.source_workspace_id}
        intent = {
            "source": "evaluation", "project_id": project_id,
            "workspace_id": project.source_workspace_id, "execution_binding": binding,
            "request_fingerprint": request_fingerprint,
        }
        accepted = {
            "suite": suite, "split": split, "mode": mode,
            "measurement_kind": ("provider_experiment" if mode == "live"
                                 else "scripted_mechanism_validation"),
            "billing_applicable": mode == "live",
            "profile": profile.snapshot(),
            **({"generation": generation, "provider_config_hash": provider_config_hash,
                "planning_quote": quote, "fact_response_format": fact_response_format}
               if mode == "live" else {}),
            "limits": {"steps": 12, "observed_tokens": 20_000, "case_seconds": 300},
            "runtime": runtime_revision(),
        }
        ticket = self.host.dispatch.scheduler.reserve_admission(run_id, owner, startable=True)
        try:
            await self.host._admission_check(lambda: profile.persist(self.host.skills.packages))
            self.store.admit_run(run_id, owner, [
                ("run.admitted", intent, "system", "runtime"),
                ("evaluation.accepted", accepted, "system", "runtime"),
            ])
            self.host._admission_done[run_id] = _Admission(owner, asyncio.Event())
            self.host._admissions[self.host._execution_key(run_id, intent)] = run_id
            self.host.dispatch.admitted(ticket)
        except BaseException as exc:
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.scheduler.release_ticket(ticket)
            raise
        finally:
            self.host.dispatch.dispatch()
        return run_id

    def start(self, run_id: str) -> asyncio.Task[RunResult]:
        if (not is_evaluation_run(self.store, run_id) or self.store.run_is_terminal(run_id)
                or run_id not in self.host._admission_done or self.host._closed):
            raise ValueError("Evaluation is not awaiting execution")
        if run_id in self.host._run_tasks:
            raise ValueError("Evaluation is already scheduled")
        try:
            task = self.host.dispatch.start(run_id, lambda: self._execute(run_id))
        except BaseException as exc:
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.finished(run_id)
            raise
        self.host._track_run_task(run_id, task)
        return task

    def _accepted(self, run_id: str) -> dict[str, Any]:
        if not is_evaluation_run(self.store, run_id):
            raise ValueError("Evaluation not found")
        return next(e["payload"] for e in self.store.run_events(run_id)
                    if e["type"] == "evaluation.accepted")

    def _directory(self, run_id: str, *, create: bool = False) -> Path:
        root = self.host.state_dir.resolve()
        namespace = root / "evaluations"
        directory = namespace / run_id
        for path in (namespace, directory):
            if path.is_symlink() or path.resolve() != path:
                raise ValueError("Evaluation artifact path changed")
            if create:
                path.mkdir(exist_ok=True)
        return directory

    async def _execute(self, run_id: str) -> RunResult:
        from eventide.evaluation import run_evaluations

        accepted = self._accepted(run_id)
        identity = self.store.get_run_identity(run_id)
        assert identity is not None
        owner = identity["session_id"]
        intent = self.host._admitted_intent(run_id)
        assert intent is not None
        start = time.perf_counter()
        async with self.host._session_execution(owner, run_id):
            self.store.append_event(run_id, "run.started", {
                **intent, "provider": self.host.settings.provider if accepted["mode"] == "live"
                else "scripted", "model": self.host.settings.model if accepted["mode"] == "live"
                else "scripted", "mode": accepted["mode"],
            })
            try:
                directory = self._directory(run_id, create=True)
                profile = EvaluationProfile.restore(self.host.skills.packages,
                                                    accepted.get("profile"))
                suite = accepted["suite"]
                cases = suite["cases"]
                encoded = json.dumps(cases, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")
                if hashlib.sha256(encoded).hexdigest() != suite["inputs_sha256"]:
                    raise ValueError("Frozen evaluation inputs do not match admission")

                def progress(kind: str, payload: dict[str, Any]) -> None:
                    if kind == "report_saved":
                        # Immutable report snapshots; HTTP validates against the exact
                        # main-Store fact, never an overwritten latest JSON file.
                        self._directory(run_id)
                        path = directory / f"report-{uuid.uuid4().hex}.json"
                        evidence = atomic_json(path, payload)
                        self.store.append_event(run_id, "evaluation.report_saved", {
                            "name": path.name, "sha256": evidence["sha256"],
                        })
                    elif kind == "case_started":
                        self.store.append_event(run_id, "evaluation.case_started", payload)
                    elif kind == "case_completed":
                        self.store.append_event(run_id, "evaluation.case_completed", {
                            key: payload[key] for key in
                            ("id", "passed", "status", "reasons", "duration_ms")
                        })
                    else:
                        raise ValueError("Unknown evaluation progress fact")

                # Construct from constants, never saved configuration or the environment.
                settings = Settings(
                    workdir=directory, state_dir=directory / "unused-state",
                    provider="scripted", api_key=None, base_url=None, model="scripted",
                    max_steps=12, run_token_limit=20_000,
                )
                call_authority = None
                if accepted["mode"] == "live":
                    if self.host.provider_config_status()["config_hash"] != accepted[
                        "provider_config_hash"
                    ]:
                        raise ValueError("Frozen evaluation provider configuration changed")
                    settings = replace(
                        self.host.settings, workdir=directory,
                        state_dir=directory / "unused-state",
                        max_steps=12, run_token_limit=20_000,
                        embedding_provider="hash", embedding_model=None,
                        embedding_api_key=None, embedding_base_url=None,
                    )
                    call_authority = (self.host, run_id)
                report = await run_evaluations(
                    Path(f"builtin:{suite['id']}"), settings=settings,
                    live=accepted["mode"] == "live", call_authority=call_authority,
                    cases_snapshot=cases,
                    suite_metadata={
                        **{key: value for key, value in suite.items() if key != "cases"},
                        "admission_runtime": accepted["runtime"],
                    },
                    artifact_parent=directory, progress=progress,
                    profile=profile,
                    split=None if accepted["split"] == "all" else accepted["split"],
                )
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                self.store.append_event(run_id, "run.interrupted" if cancelled else "run.failed", {
                    "error": "Evaluation cancelled" if cancelled else self.host._clean(str(exc)),
                    "reason": "cancelled" if cancelled else "evaluation_failed",
                })
                raise
            label = "Live experiment" if accepted["mode"] == "live" else "Offline mechanisms"
            output = f"{label}: {report['passed']}/{report['total']} passed"
            terminal = "completed" if report["status"] == "completed" else "interrupted"
            duration = (time.perf_counter() - start) * 1000
            self.store.append_event(run_id, "evaluation.finished", {
                "passed": report["passed"], "total": report["total"],
            })
            self.store.append_event(run_id, "run." + terminal, {
                "output": output, "steps": 0, "tool_calls": 0, "usage": {},
                **({"reason": "evaluation_aborted"} if terminal != "completed" else {}),
                "duration_ms": duration,
            })
            return RunResult(run_id, owner, terminal, output, 0, 0, duration,
                             turn_id=identity["turn_id"])

    def detail(self, run_id: str) -> dict[str, Any]:
        accepted = self._accepted(run_id)
        run = self.store.get_run_summary(run_id)
        assert run is not None
        suite = accepted["suite"]
        events = self.store.run_events(run_id)
        started = {e["payload"]["id"] for e in events if e["type"] == "evaluation.case_started"}
        completed = {e["payload"]["id"]: e["payload"] for e in events
                     if e["type"] == "evaluation.case_completed"}
        cases = []
        for case in suite["cases"]:
            result = completed.get(case["id"])
            status = (result["status"] if result else "interrupted" if case["id"] in started
                      and run["status"] in {"failed", "interrupted"} else
                      "running" if case["id"] in started else "pending")
            if status == "cancelled":
                status = "interrupted"
            cases.append({
                "id": case["id"], "category": case["benchmark"]["category"],
                "split": case["benchmark"]["split"], "status": status,
                "passed": result["passed"] if result else None,
                "reasons": result["reasons"] if result else [],
                "duration_ms": result["duration_ms"] if result else None,
            })
        project_id = self.host.sessions.workspace(run["session_id"]).project_id
        queue = self.host.dispatch.snapshot(project_id)
        entry = next((item for item in queue["active"] + queue["queued"]
                      if item["run_id"] == run_id), None)
        return {
            "evaluation_id": run_id, "run_id": run_id, "session_id": run["session_id"],
            "project_id": project_id, "suite_id": suite["id"], "suite_version": suite["version"],
            "suite_sha256": suite["suite_sha256"], "split": accepted["split"],
            "mode": accepted["mode"], "measurement_kind": accepted["measurement_kind"],
            "preset": accepted.get("profile", {}).get("preset", "none"),
            "profile": accepted.get("profile", EvaluationProfile().snapshot()),
            "billing_applicable": accepted["billing_applicable"], "status": run["status"],
            "dispatch_state": entry["dispatch_state"] if entry else None,
            "queue_position": entry["position"] if entry else None,
            "total": len(cases), "attempted": len(completed),
            "passed": sum(c["passed"] is True for c in cases),
            "failed": sum(c["passed"] is False for c in cases),
            "remaining": len(cases) - len(completed),
            "current_case": next((c["id"] for c in cases if c["status"] == "running"), None),
            "cases": cases,
            "report_available": any(e["type"] == "evaluation.report_saved" for e in events),
            "report_url": f"/api/evaluations/{run_id}/report",
            "events_url": f"/api/runs/{run_id}/events", "error": run["error"],
        }

    def list(self, project_id: str, *, limit: int = 20, before: str | None = None
             ) -> list[dict[str, Any]]:
        owner = self.owner(project_id, create=False)
        return [self.detail(run["id"]) for run in reversed(
            self.store.list_runs(owner, limit=limit, before=before)
        )]

    async def cancel(self, run_id: str, request_key: str) -> dict[str, Any]:
        self._accepted(run_id)
        if self.store.run_is_terminal(run_id):
            return self.detail(run_id)
        marker = hashlib.sha256(request_key.encode()).hexdigest()
        if not any(e["type"] == "evaluation.cancel_requested" and
                   e["payload"].get("request") == marker for e in self.store.run_events(run_id)):
            self.store.append_event(run_id, "evaluation.cancel_requested", {"request": marker})
        try:
            await self.host.cancel_run(run_id)
        except ValueError:
            # A concurrent exact cancellation may already have finished the task.
            if not self.store.run_is_terminal(run_id):
                raise
        return self.detail(run_id)

    def report(self, run_id: str) -> dict[str, Any]:
        self._accepted(run_id)
        evidence = next((e["payload"] for e in reversed(self.store.run_events(run_id))
                         if e["type"] == "evaluation.report_saved"), None)
        if evidence is None:
            raise FileNotFoundError("Evaluation report is not available")
        if not re.fullmatch(r"report-[a-f0-9]{32}\.json", evidence["name"]):
            raise ValueError("Invalid evaluation report reference")
        path = self._directory(run_id) / evidence["name"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Evaluation report is missing or changed")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != evidence["sha256"]:
            raise ValueError("Evaluation report fingerprint changed")
        report: dict[str, Any] = json.loads(body)
        return report
