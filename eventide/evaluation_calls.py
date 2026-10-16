"""Isolated evaluations reference the main Runtime's sole model-call ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eventide.budget_prices import summarize_cost_events
from eventide.eval_evidence import atomic_json
from eventide.usage import build_usage_ledger

if TYPE_CHECKING:
    from pathlib import Path

    from eventide.host import EventSink, RuntimeHost
    from eventide.models import ModelRequest, ModelResponse


class EvaluationCallBridge:
    def __init__(self, authority: RuntimeHost, evaluation_id: str, case_id: str, index: int):
        if not case_id or len(case_id) > 256 or type(index) is not int or index < 1:
            raise ValueError("Invalid evaluation case identity")
        self.authority = authority
        self.evaluation_id = evaluation_id
        self.case_id = case_id
        self.index = index

    def _validate(self) -> None:
        from eventide.evaluation_control import is_evaluation_run

        if not is_evaluation_run(self.authority.store, self.evaluation_id):
            raise ValueError("Evaluation model authority is missing")
        accepted: dict[str, Any] = next((e["payload"] for e in self.authority.store.run_events(
            self.evaluation_id,
        ) if e["type"] == "evaluation.accepted"), {})
        if accepted.get("mode") != "live":
            raise ValueError("Offline evaluation cannot dispatch model requests")
        if self.authority.store.run_is_terminal(self.evaluation_id):
            raise ValueError("Evaluation model authority is already terminal")

    async def complete(
        self, runtime: RuntimeHost, request: ModelRequest, run_id: str,
        sink: EventSink | None, *, kind: str, max_attempts: int,
    ) -> ModelResponse:
        self._validate()
        if self.authority._evaluation_bridge is not None:
            raise ValueError("Nested evaluation model authority is not supported")
        if runtime.store.facts_by_type(("delivery.repair_started",)):
            raise ValueError("Controlled evaluations do not yet support bounded repair batches")
        if runtime is self.authority or runtime._evaluation_bridge is not self:
            raise ValueError("Evaluation model bridge ownership mismatch")
        if (runtime.settings.provider, runtime.settings.base_url, runtime.settings.model) != (
            self.authority.settings.provider, self.authority.settings.base_url,
            self.authority.settings.model,
        ):
            raise ValueError("Evaluation provider configuration changed")
        identity = runtime.store.get_run_identity(run_id)
        if identity is None:
            raise ValueError("Evaluation local Run is missing")
        intent = runtime._admitted_intent(run_id) or {}
        purpose = "worker" if kind == "agent" and intent.get("parent_run_id") else kind
        origin = {
            "case_id": self.case_id, "case_index": self.index,
            "local_run_id": run_id, "local_session_id": identity["session_id"],
        }
        # Only reference the remote authority here; never append a second model attempt.
        runtime.store.append_event(run_id, "evaluation.model_request", {
            "authority_run_id": self.evaluation_id, "kind": purpose, **origin,
        })
        return await self.authority._complete_with_retry(
            request, self.evaluation_id, None, kind=purpose,
            max_attempts=max_attempts, _evaluation_origin=origin,
        )

    def events(self) -> list[dict[str, Any]]:
        result = []
        for event in self.authority.store.run_events(self.evaluation_id):
            if event["type"] not in {"model.attempt_started", "model.attempt_finished"}:
                continue
            origin = event["payload"].get("evaluation_origin", {})
            if origin.get("case_index") == self.index and origin.get("case_id") == self.case_id:
                result.append(event)
        return result

    def usage_events(self, runtime: RuntimeHost, run_id: str) -> list[dict[str, Any]]:
        if runtime.store.get_run_identity(run_id) is None:
            raise ValueError("Evaluation local Run is missing")
        identities = {run_id}
        changed = True
        events = []
        for workspace in runtime.store.list_workspaces():
            for session in runtime.store.list_sessions(workspace.workspace_id):
                events.extend(runtime.store.session_events(session["id"]))
        while changed:
            changed = False
            for event in events:
                if (event["type"] in {"run.admitted", "run.started"}
                        and event["payload"].get("parent_run_id") in identities
                        and event["run_id"] not in identities):
                    identities.add(event["run_id"])
                    changed = True
        return [event for event in self.events()
                if event["payload"]["evaluation_origin"]["local_run_id"] in identities]

    def evidence(self, directory: Path) -> dict[str, Any]:
        events = self.events()
        attempt_ids = {event["payload"]["attempt_id"] for event in events}
        costs = [event for event in self.authority.store.facts_by_type(
            ("cost.estimated", "budget.reserved"),
        ) if event["payload"].get("attempt_id") in attempt_ids]
        versions = {
            line["price_version"] for event in costs if event["type"] == "cost.estimated"
            for estimate in event["payload"]["estimates"].values()
            for line in estimate["lines"]
        }
        prices = [event["payload"] for event in self.authority.store.facts_by_type(
            ("price.registered",),
        ) if event["payload"]["version_id"] in versions]
        exported = atomic_json(directory / "model-authority-events.json", {
            "events": events, "cost_events": costs, "price_versions": prices,
        })
        return {
            "source": "eventide.main_runtime_event_log",
            "database": str(self.authority.store.path),
            "authority_run_id": self.evaluation_id,
            "case_id": self.case_id, "case_index": self.index,
            "events": exported, "usage": build_usage_ledger(events),
            "model_attempts": [{"run_id": e["run_id"], **e["payload"]} for e in events
                               if e["type"] == "model.attempt_started"],
            "costs": summarize_cost_events(costs), "price_versions": prices,
        }
