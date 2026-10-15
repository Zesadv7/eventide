"""Request admission and settlement on the Runtime's sole model-call path.

Serialization bytes are an explicit conservative heuristic, never observed Token
usage or a provider billing bound. No prompt, attachment data or key is persisted
by the estimator. Provider usage remains the sole automatic settlement evidence.
"""

from __future__ import annotations

import json
from typing import Any

from eventide.budget_prices import CostLedger
from eventide.budgets import (
    BudgetContext,
    BudgetLedger,
    ReservationReceipt,
    is_service_probe_run,
)
from eventide.input_composition import measure_input_composition
from eventide.models import ModelRequest
from eventide.store import RuntimeStore


def estimate_request(request: ModelRequest) -> tuple[int, int, str]:
    serialized = json.dumps(
        {"system": request.system, "messages": request.messages, "tools": request.tools},
        ensure_ascii=False,
    )
    # Byte length deliberately avoids claiming a universal characters-per-token
    # ratio. Images are counted as serialized data, so this can over-reserve.
    return len(serialized.encode("utf-8")), request.max_tokens, "serialized_utf8_bytes_v1"


class ModelBudgetController:
    def __init__(self, store: RuntimeStore):
        self.store = store
        self.ledger = BudgetLedger(store)
        self.costs = CostLedger(store)

    def context(self, run_id: str) -> tuple[str, BudgetContext]:
        """Resolve trusted identities, including workers and delivery Continue."""
        run = self.store.get_run_identity(run_id)
        if run is None:
            raise ValueError("Run not found")
        session_id = run["session_id"]
        if is_service_probe_run(self.store, run_id):
            return session_id, BudgetContext(None, run_id)
        ancestor = run_id
        visited: set[str] = set()
        project_id: str | None = None
        while True:
            if ancestor in visited:
                raise ValueError("Cyclic parent Run identity")
            visited.add(ancestor)
            identity = self.store.get_run_identity(ancestor)
            session = identity and self.store.get_session(identity["session_id"])
            if session is None:
                raise ValueError("Missing budget owner")
            workspace = self.store.get_workspace(session["workspace_id"])
            intents = [event["payload"] for event in self.store.run_events(ancestor)
                       if event["type"] in {"run.admitted", "run.started"}]
            if not intents:
                raise ValueError("Run has no admitted budget owner")
            for intent in intents:
                if intent.get("project_id", workspace.project_id) != workspace.project_id:
                    raise ValueError("Admitted project identity mismatch")
            if project_id is not None and project_id != workspace.project_id:
                raise ValueError("Child Run changed project ownership")
            project_id = workspace.project_id
            parents = {intent["parent_run_id"] for intent in intents
                       if intent.get("parent_run_id")}
            if len(parents) > 1:
                raise ValueError("Conflicting parent Run identity")
            if parents:
                ancestor = parents.pop()
                continue
            delivery_ids = {
                intent.get("execution_config", {}).get("writer_delivery_id")
                for intent in intents
                if intent.get("execution_config", {}).get("writer_delivery_id")
            }
            if len(delivery_ids) > 1:
                raise ValueError("Conflicting delivery identity")
            delivery_id = next(iter(delivery_ids), None)
            if delivery_id is not None:
                prepared = [event["payload"] for event in self.store.delivery_facts(delivery_id)
                            if event["type"] == "delivery.prepared"]
                if len(prepared) != 1:
                    raise ValueError("Missing delivery budget owner")
                source = self.store.get_workspace(prepared[0]["workspace_id"])
                if source.project_id != project_id:
                    raise ValueError("Delivery project identity mismatch")
                if prepared[0]["receipt"]["checkout"] != workspace.path:
                    raise ValueError("Run does not own the delivery checkout")
            return session_id, BudgetContext(project_id, ancestor, delivery_id)

    def begin(
        self, run_id: str, request: ModelRequest, identity: dict[str, Any],
    ) -> ReservationReceipt:
        session_id, context = self.context(run_id)
        inputs, outputs, method = estimate_request(request)
        return self.ledger.reserve(
            session_id, run_id, identity["attempt_id"], context=context,
            input_estimate=inputs, output_estimate=outputs, estimate_method=method,
            attempt_started={
                **identity, "input_composition": measure_input_composition(request),
            },
        )

    def finish(self, run_id: str, identity: dict[str, Any]) -> ReservationReceipt:
        run = self.store.get_run_identity(run_id)
        if run is None:
            raise ValueError("Run not found")
        session_id = run["session_id"]
        usage = identity.get("usage")
        receipt = self.ledger.settle(
            session_id, identity["attempt_id"], usage,
            source="reported" if usage else "unknown",
            run_id=run_id, attempt_finished=identity,
        )
        # The cost record is a snapshot of committed settlement evidence. A crash
        # before this append leaves an explicitly unpriced call, never free usage.
        self.costs.estimate_cost(
            session_id, identity["attempt_id"], provider=identity["provider"],
            model=identity["model"], estimate_id=f"cost_{identity['attempt_id']}",
        )
        return receipt
