"""Same-origin mutation checks and durable Run request identity helpers."""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from eventide.workspace import digest

if TYPE_CHECKING:
    from eventide.host import RuntimeHost

MUTATION_OUTCOME_HEADER = "X-Eventide-Mutation-Outcome"


def check_mutation_origin(request: Request, *, require_origin: bool = False) -> None:
    """Non-browser local clients may omit Origin; browser mutations must be same-origin."""
    if not require_origin and request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site mutation is not allowed")
    if origin is None:
        if require_origin:
            raise HTTPException(403, "Local container forwarding requires an Origin header")
        return
    source = urlparse(origin)
    target = urlparse(str(request.base_url))
    try:
        local_target = ipaddress.ip_address(target.hostname or "").is_loopback
    except ValueError:
        local_target = target.hostname == "localhost" or (
            target.hostname == "testserver"
            and request.client is not None
            and request.client.host == "testclient"
        )
    if not local_target:
        raise HTTPException(403, "Browser mutations require a local Eventide origin")
    if source.scheme not in {"http", "https"} or source.username or source.password:
        raise HTTPException(403, "Invalid mutation origin")
    if source.path not in {"", "/"} or source.query or source.fragment:
        raise HTTPException(403, "Invalid mutation origin")
    source_port = source.port or (443 if source.scheme == "https" else 80)
    target_port = target.port or (443 if target.scheme == "https" else 80)
    if (source.scheme, source.hostname, source_port) != (
        target.scheme,
        target.hostname,
        target_port,
    ):
        raise HTTPException(403, "Mutation origin must match the Eventide service")


@dataclass(frozen=True, slots=True)
class RunReceipt:
    run_id: str
    fingerprint: str | None
    existing: bool = False


def run_receipt(
    runtime: RuntimeHost,
    request: Request,
    session_id: str,
    operation: str,
    payload: dict[str, Any],
) -> RunReceipt:
    key = request.headers.get("idempotency-key")
    if key is None:
        return RunReceipt(f"run_{uuid.uuid4().hex[:16]}", None)
    if not key or len(key) > 128 or any(ord(char) <= 32 or ord(char) >= 127 for char in key):
        raise HTTPException(422, "Idempotency-Key must be 1–128 visible ASCII characters")
    run_id = "run_" + digest([session_id, operation, key])[:32]
    fingerprint = digest([session_id, operation, payload])
    identity = runtime.store.get_run_identity(run_id)
    if identity is None:
        return RunReceipt(run_id, fingerprint)
    admitted: dict[str, Any] = next(
        (
            event["payload"]
            for event in runtime.store.run_events(run_id)
            if event["type"] == "run.admitted"
        ),
        {},
    )
    if identity["session_id"] != session_id or admitted.get("request_fingerprint") != fingerprint:
        raise HTTPException(409, "Idempotency-Key was already used with different content")
    return RunReceipt(run_id, fingerprint, True)


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    identity: str
    owner: str
    result: dict[str, Any] | None = None


def mutation_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if not key or len(key) > 128 or any(ord(char) <= 32 or ord(char) >= 127 for char in key):
        raise HTTPException(422, "Idempotency-Key must be 1–128 visible ASCII characters")
    return key


def begin_mutation(
    runtime: RuntimeHost,
    request: Request,
    owner: str,
    operation: str,
    payload: dict[str, Any],
) -> MutationReceipt:
    """Persist an at-most-once local mutation; ambiguous writes are never replayed."""
    key = mutation_key(request)
    identity = "mutation_" + digest([owner, operation, key])
    fingerprint = digest(payload)
    events = [
        e
        for e in runtime.store.session_events(owner)
        if e["type"].startswith("http.mutation_") and e["payload"].get("identity") == identity
    ]
    if events:
        if events[0]["payload"].get("fingerprint") != fingerprint:
            raise HTTPException(409, "Idempotency-Key was already used with different content")
        terminal = events[-1]
        if terminal["type"] == "http.mutation_completed":
            return MutationReceipt(identity, owner, terminal["payload"]["result"])
        if terminal["type"] == "http.mutation_failed":
            rejected = terminal["payload"].get("outcome") == "rejected"
            raise HTTPException(
                terminal["payload"]["status_code"], terminal["payload"]["message"],
                headers={MUTATION_OUTCOME_HEADER: "rejected"} if rejected else None,
            )
        raise HTTPException(
            409, "Mutation is running or its outcome is unknown; inspect current state"
        )
    runtime.store.append_fact(
        owner,
        "http.mutation_started",
        {
            "identity": identity,
            "operation": operation,
            "fingerprint": fingerprint,
        },
        author="user",
    )
    return MutationReceipt(identity, owner)


def finish_mutation(
    runtime: RuntimeHost,
    receipt: MutationReceipt,
    result: dict[str, Any],
) -> dict[str, Any]:
    runtime.store.append_fact(
        receipt.owner,
        "http.mutation_completed",
        {
            "identity": receipt.identity,
            "result": result,
        },
        author="user",
    )
    return result


def fail_mutation(
    runtime: RuntimeHost,
    receipt: MutationReceipt,
    message: str,
    status_code: int = 422,
    *,
    rejected: bool = False,
) -> None:
    """Record failure; opt in to rejection only when the operation had no effect.

    A failure alone does not prove rollback. Existing callers and ambiguous
    filesystem/network failures must retain their unknown-outcome semantics.
    """
    runtime.store.append_fact(
        receipt.owner,
        "http.mutation_failed",
        {
            "identity": receipt.identity,
            "message": message,
            "status_code": status_code,
            **({"outcome": "rejected"} if rejected else {}),
        },
        author="user",
    )
