"""Atomic project registration/removal receipts; never delete source files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from eventide.host import RuntimeHost
from eventide.http_control import begin_mutation, fail_mutation, mutation_key
from eventide.models import WorkspaceRecord
from eventide.workspace import canonical_workspace


def mutate_workspace(
    host: RuntimeHost, request: Request, *, path: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    mutation_key(request)
    owner = host.configuration_owner()
    removing = workspace_id is not None
    receipt = begin_mutation(
        host, request, owner, "workspace_remove" if removing else "workspace_register",
        {"workspace_id": workspace_id} if removing else {"path": path},
    )
    if receipt.result is not None:
        return receipt.result
    result: dict[str, Any] = {}

    def committed(record: WorkspaceRecord | None = None) -> None:
        if record is not None:
            result.update(host.workspace_status(record.workspace_id))
        else:
            result["removed"] = workspace_id
        host.store._append_fact_locked(
            owner, "workspace.removed" if removing else "workspace.registered",
            {"workspace_id": workspace_id if removing else result["workspace_id"]},
            author="user",
        )
        host.store._append_fact_locked(
            owner, "http.mutation_completed",
            {"identity": receipt.identity, "result": result}, author="user",
        )

    try:
        if workspace_id is not None:
            if workspace_id == host.default_workspace.workspace_id:
                raise ValueError("Service startup Workspace cannot be removed")
            if host.workspace_busy(workspace_id):
                raise ValueError("Workspace is busy")
            host.store.remove_workspace(workspace_id, on_removed=committed)
        else:
            root, git_root = canonical_workspace(Path(path or ""))
            host._check_worktree_admission(root)
            host.store.register_workspace(root, git_root, on_registered=committed)
        return result
    except (ValueError, OSError) as exc:
        message = str(exc)
        code = 404 if message.startswith("Workspace not found") else (409 if removing else 422)
        fail_mutation(host, receipt, message, code, rejected=True)
        raise HTTPException(
            code, message, headers={"X-Eventide-Mutation-Outcome": "rejected"},
        ) from exc
    except Exception:
        raise HTTPException(
            503, "Workspace mutation outcome is unknown; retry the original identity",
        ) from None
