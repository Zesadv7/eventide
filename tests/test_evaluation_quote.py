"""A planning estimate is read-only, versioned, and never presented as a hard bill cap."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.budget_prices import PriceVersion
from eventide.evaluation_quote import evaluation_quote
from tests.test_evaluation_control import host_for


def test_quote_unknown_prices_exact_currencies_and_stale_versions(tmp_path):
    host = host_for(tmp_path)
    project = host.default_workspace.project_id
    try:
        before = host.store.facts_by_type(("control.session", "price.registered", "run.admitted"))
        quote = evaluation_quote(host, project)
        assert quote["planning_tokens"] == {"input_tokens": 216_000, "output_tokens": 24_000}
        assert quote["complete"] is False and quote["estimates"] == {}
        assert evaluation_quote(host, project) == quote
        assert (
            host.store.facts_by_type(("control.session", "price.registered", "run.admitted"))
            == before
        )
        for currency, categories in (
            ("CNY", ("input_tokens", "output_tokens")),
            ("USD", ("input_tokens",)),
        ):
            for category in categories:
                host.register_price(
                    PriceVersion(
                        currency + category,
                        "scripted",
                        "scripted",
                        category,
                        currency,
                        "2",
                        datetime(2020, 1, 1, tzinfo=timezone.utc),
                    )
                )
        priced = evaluation_quote(host, project)
        assert priced["quote_hash"] != quote["quote_hash"]
        assert priced["estimates"]["CNY"]["known_amount"] == "0.480"
        assert priced["estimates"]["CNY"]["complete"] is True
        assert priced["estimates"]["USD"]["known_amount"] == "0.432"
        assert priced["estimates"]["USD"]["complete"] is False
        assert priced["complete"] is False
        assert host.provider.requests == []
        with pytest.raises(ValueError, match="split"):
            evaluation_quote(host, project, split="invalid")
    finally:
        host.store.close()
        host._lease.close()


def test_http_live_requires_reviewed_quote_and_replays_before_revalidation(tmp_path):
    import time

    from eventide.budgets import BudgetScope

    host = host_for(tmp_path)
    project = host.default_workspace.project_id
    host.configure_budget(BudgetScope("project", project), token_limit=0, request_id="no-spend")
    with TestClient(create_app(host)) as client:
        path = f"/api/projects/{project}/evaluations"
        quote = client.get(path + "/quote?split=development").json()
        body = {
            "suite_id": quote["suite_id"],
            "suite_sha256": quote["suite_sha256"],
            "split": quote["split"],
            "mode": "live",
            "quote_hash": quote["quote_hash"],
            "confirmed": True,
        }
        assert client.post(path, json=body).status_code == 422
        assert (
            client.post(
                path,
                json=body,
                headers={"Idempotency-Key": "cross", "Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        host.configure_generation(host.settings.model, {"max_output_tokens": 32})
        stale = client.post(path, json=body, headers={"Idempotency-Key": "stale"})
        assert stale.status_code == 409
        assert stale.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        assert client.get(path).json() == []
        quote = client.get(path + "/quote?split=development").json()
        body["quote_hash"] = quote["quote_hash"]
        key = {"Idempotency-Key": "reviewed"}
        accepted = client.post(path, json=body, headers=key)
        assert accepted.status_code == 202
        identity = accepted.json()
        for _ in range(300):
            detail = client.get(identity["status_url"]).json()
            if detail["status"] not in {"running", "queued"}:
                break
            time.sleep(0.01)
        assert detail["status"] == "interrupted"
        assert detail["mode"] == "live"
        host.configure_generation(host.settings.model, {"max_output_tokens": 64})
        assert client.post(path, json=body, headers=key).json() == identity
        assert len(client.get(path).json()) == 1
        assert host.provider.requests == []
