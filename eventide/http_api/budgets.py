"""Budget control and scoped fee estimates; all state remains in Event Log."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventide.budget_prices import PriceVersion
from eventide.budget_views import (
    ViewKind,
    budget_view,
    control_owner,
    cost_view,
    validate_owner,
)
from eventide.budgets import BUDGET_FACTS, BudgetScope, ScopeKind
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import begin_mutation, fail_mutation, finish_mutation


class BudgetBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    token_limit: int | None = Field(ge=0, le=1_000_000_000_000)
    expected_configuration_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class PriceBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version_id: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    category: Literal[
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens",
    ]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    price_per_million: str = Field(min_length=1, max_length=64)
    effective_at: str = Field(min_length=1, max_length=64)

    def price(self) -> PriceVersion:
        at = datetime.fromisoformat(self.effective_at.replace("Z", "+00:00"))
        return PriceVersion(
            self.version_id, self.provider, self.model, self.category,
            self.currency, self.price_per_million, at,
        )

    @model_validator(mode="after")
    def validate_price(self) -> PriceBody:
        self.price().payload()
        return self


class UnknownBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    action: Literal["record_known", "keep_unknown"]
    actual_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
    reason: str = Field(min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("An explicit reason is required")
        return value.strip()

    @model_validator(mode="after")
    def validate_action(self) -> UnknownBody:
        if self.action == "record_known" and self.actual_tokens is None:
            raise ValueError("record_known requires actual_tokens")
        if self.action == "keep_unknown" and "actual_tokens" in self.model_fields_set:
            raise ValueError("keep_unknown cannot supply actual_tokens")
        return self


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["budgets"])
    host = context.runtime

    def check_owner(kind: ViewKind, identity: str) -> None:
        try:
            validate_owner(host, kind, identity)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def attempt_view(attempt_id: str) -> dict[str, Any]:
        attempt = next((item for item in budget_view(host)["reservations"]
                        if item["attempt_id"] == attempt_id), None)
        if attempt is None:
            raise HTTPException(404, "Attempt reservation not found")
        return dict(attempt)

    def require_unknown(attempt_id: str) -> dict[str, Any]:
        attempt = attempt_view(attempt_id)
        if attempt["status"] != "unsettled":
            raise HTTPException(409, "Attempt is already settled")
        finished = any(
            e["type"] == "model.attempt_finished"
            and e["payload"].get("attempt_id") == attempt_id
            for e in host.store.run_events(attempt["run_id"])
        )
        if not finished and not host.store.run_is_terminal(attempt["run_id"]):
            raise HTTPException(409, "Wait for the attempt to finish before reconciliation")
        return attempt

    @router.get("/api/budgets")
    async def budgets() -> dict[str, Any]:
        return budget_view(host)

    @router.get("/api/budgets/{kind}/{identity}")
    async def scoped_budget(kind: ScopeKind, identity: str) -> dict[str, Any]:
        check_owner(kind, identity)
        return budget_view(host, BudgetScope(kind, identity))

    @router.put("/api/budgets/{kind}/{identity}")
    async def configure(
        kind: ScopeKind, identity: str, body: BudgetBody, request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        check_owner(kind, identity)
        receipt = begin_mutation(host, request, control_owner(host), "budget.configure", {
            "kind": kind, "identity": identity, **body.model_dump(),
        })
        if receipt.result is not None:
            return receipt.result
        scope = BudgetScope(kind, identity)
        current = budget_view(host, scope)["budgets"][0]
        if body.expected_configuration_hash is not None and (
            body.expected_configuration_hash != current["configuration_hash"]
        ):
            message = "Budget configuration changed; refresh before saving"
            fail_mutation(host, receipt, message, 409)
            raise HTTPException(409, message)
        try:
            host.configure_budget(scope, token_limit=body.token_limit, request_id=receipt.identity)
        except ValueError as exc:
            fail_mutation(host, receipt, str(exc), 409)
            raise HTTPException(409, str(exc)) from exc
        return finish_mutation(host, receipt, budget_view(host, scope))

    @router.get("/api/prices")
    async def prices(
        provider: str | None = None, model: str | None = None, currency: str | None = None,
    ) -> dict[str, Any]:
        filters = {"provider": provider, "model": model, "currency": currency}
        return {"prices": [
            e["payload"] for e in host.store.facts_by_type(("price.registered",))
            if all(value is None or e["payload"][key] == value for key, value in filters.items())
        ]}

    @router.post("/api/prices")
    async def register_price(body: PriceBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        receipt = begin_mutation(
            host, request, control_owner(host), "price.register", body.model_dump(),
        )
        if receipt.result is not None:
            return receipt.result
        try:
            result = host.register_price(body.price())
        except ValueError as exc:
            fail_mutation(host, receipt, str(exc), 409)
            raise HTTPException(409, str(exc)) from exc
        return finish_mutation(host, receipt, result)

    @router.get("/api/costs")
    async def costs(
        scope_kind: ViewKind | None = None, scope_id: str | None = None,
        include_children: bool = True, date_from: str | None = None, date_to: str | None = None,
    ) -> dict[str, Any]:
        try:
            return cost_view(
                host, kind=scope_kind, identity=scope_id, include_children=include_children,
                date_from=date_from, date_to=date_to,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/budgets/attempts/{attempt_id}/resolve")
    async def resolve_unknown(
        attempt_id: str, body: UnknownBody, request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        attempt_view(attempt_id)
        receipt = begin_mutation(host, request, control_owner(host), "budget.resolve", {
            "attempt_id": attempt_id, **body.model_dump(),
        })
        if receipt.result is not None:
            return receipt.result
        try:
            attempt = require_unknown(attempt_id)
            reason = host._semantic(body.reason)
            if body.action == "record_known":
                assert body.actual_tokens is not None
                observed = max((
                    sum(e["payload"]["usage"].get(key, 0)
                        for key in ("input_tokens", "output_tokens"))
                    for e in host.store.facts_by_type(("budget.settled",))
                    if e["payload"]["attempt_id"] == attempt_id
                    and e["payload"]["usage_source"] == "reported"
                ), default=0)
                if body.actual_tokens < observed:
                    raise HTTPException(422, "Actual total cannot be below reported usage")
                host.model_budget.ledger.resolve_unknown(
                    attempt["session_id"], attempt_id, actual_tokens=body.actual_tokens,
                    reason=reason, request_id=receipt.identity,
                )
            else:
                def acknowledge(_events: list[dict[str, Any]]) -> tuple[None, list[Any]]:
                    require_unknown(attempt_id)
                    return None, [("budget.unknown_acknowledged", {
                        "attempt_id": attempt_id, "request_id": receipt.identity,
                        "reason": reason, "action": "keep_unknown",
                    })]

                host.store.transact_facts(
                    attempt["session_id"], BUDGET_FACTS, acknowledge, author="user",
                )
        except (HTTPException, ValueError) as exc:
            code = exc.status_code if isinstance(exc, HTTPException) else 409
            message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            fail_mutation(host, receipt, message, code)
            raise HTTPException(code, message) from exc
        return finish_mutation(host, receipt, {
            "action": body.action, "attempt": attempt_view(attempt_id),
        })

    return router
