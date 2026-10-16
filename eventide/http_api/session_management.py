"""Versioned Session metadata writes with atomic success receipts."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from eventide.host import RuntimeHost
from eventide.http_control import begin_mutation, fail_mutation, mutation_key


def mutate_session(
    host: RuntimeHost,
    request: Request,
    session_id: str,
    body: dict[str, Any],
    *,
    delete: bool = False,
) -> dict[str, Any]:
    mutation_key(request)
    owner = host.configuration_owner()
    receipt = begin_mutation(
        host,
        request,
        owner,
        "session_delete" if delete else "session_update",
        {"session_id": session_id, **body},
    )
    if receipt.result is not None:
        return receipt.result
    try:
        if host.store.get_session(session_id) is None:
            raise ValueError("Session not found")
        if host.is_control_session(session_id):
            raise ValueError("Control Session metadata is managed by the service")
        if delete and host.session_busy(session_id):
            raise ValueError("Session is busy")
        return host.store.commit_session_metadata(
            owner,
            session_id,
            body["expected_metadata_hash"],
            mutation_id=receipt.identity,
            delete=delete,
            title=body.get("title"),
            archived=body.get("archived"),
            result=(lambda: {"deleted": session_id})
            if delete
            else lambda: host.session_status(session_id),
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if message == "Session not found" else 409
        fail_mutation(host, receipt, message, code, rejected=True)
        raise HTTPException(
            code, message, headers={"X-Eventide-Mutation-Outcome": "rejected"}
        ) from exc
    except Exception:
        raise HTTPException(
            503,
            "Session mutation outcome is unknown; retry the original identity",
        ) from None


def create_session(
    host: RuntimeHost,
    request: Request,
    body: dict[str, Any],
) -> dict[str, Any]:
    mutation_key(request)
    owner = host.configuration_owner()
    receipt = begin_mutation(host, request, owner, "session_create", body)
    if receipt.result is not None:
        return receipt.result
    try:
        identity = host.create_session(
            workspace_id=body.get("workspace_id"),
            working_directory=body.get("working_directory"),
            _mutation=(owner, receipt.identity),
        )
        return {"session_id": identity}
    except (ValueError, OSError) as exc:
        message = str(exc)
        code = 404 if message.startswith("Workspace not found") else 422
        fail_mutation(host, receipt, message, code, rejected=True)
        raise HTTPException(
            code, message, headers={"X-Eventide-Mutation-Outcome": "rejected"}
        ) from exc
    except Exception:
        raise HTTPException(
            503,
            "Session creation outcome is unknown; retry the original identity",
        ) from None
