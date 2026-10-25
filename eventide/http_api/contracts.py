"""Stable Web API contract identity and real capability discovery (MK-00).

The frontend builds against one explicit ``CONTRACT_VERSION``; a backend that
serves a different version must be answered with an upgrade prompt instead of
best-effort field guessing. ``schema_version`` is read from the live runtime
store, never declared independently here. ``IMPLEMENTED_CAPABILITIES`` lists
only shipped features: add or remove an entry in the same commit that ships or
retires the corresponding capability, never for planned MK-xx work.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from eventide import __version__
from eventide.http_api.context import ApiContext
from eventide.http_api.schemas import RuntimeContractResponse

CONTRACT_VERSION = 1

# Each key maps to an accepted, implemented capability with backend permission,
# HTTP/OpenAPI projection and web delivery behind it (see docs/api-contract.md).
IMPLEMENTED_CAPABILITIES: tuple[str, ...] = (
    "projects",
    "sessions",
    "runs",
    "approvals",
    "attachments",
    "worktrees",
    "delivery",
    "file_undo",
    "skills",
    "mcp",
    "retrieval",
    "memory",
    "usage_ledger",
    "evaluations",
    "history_import",
    "docker_sandbox",
    "workbench",
)


def store_schema_version(path: Path) -> int | None:
    """Read the applied schema version of the live store without writing to it.

    ``RuntimeStore`` migrates on open and refuses unsupported versions, so any
    store this process serves reports exactly the version this code supports.
    """
    uri = f"file:{quote(str(path))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["runtime"])

    @router.get("/api/runtime/contract", response_model=RuntimeContractResponse)
    async def get_runtime_contract() -> RuntimeContractResponse:
        schema_version = store_schema_version(context.runtime.store.path)
        if schema_version is None:
            raise HTTPException(
                503, "运行库数据版本不可读，无法确认前后端兼容性"
            )
        return RuntimeContractResponse(
            contract_version=CONTRACT_VERSION,
            schema_version=schema_version,
            runtime_version=__version__,
            capabilities=list(IMPLEMENTED_CAPABILITIES),
        )

    return router
