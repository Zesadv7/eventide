"""Explicit current-version configuration requests for unrelated API regressions."""

from uuid import uuid4


def put_configuration(client, path, *, json, headers=None):
    current = client.get(path, params={"model": json["model"]} if "generation" in path else None)
    return client.put(
        path, json={**json, "expected_config_hash": current.json()["config_hash"]},
        headers={"Idempotency-Key": uuid4().hex, **(headers or {})},
    )


def reset_provider(client):
    return client.request(
        "DELETE", "/api/config/provider",
        json={"expected_config_hash": client.get("/api/config/provider").json()["config_hash"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
