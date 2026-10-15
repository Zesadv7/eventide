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


def check_mutation_origin(request: Request) -> None:
    """Non-browser local clients may omit Origin; browser mutations must be same-origin."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site mutation is not allowed")
    if origin is None:
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
