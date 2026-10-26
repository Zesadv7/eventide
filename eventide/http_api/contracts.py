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
from eventide.http_api.schemas import (
    NativeBackendStatus,
    NativeCapabilitiesResponse,
    RuntimeContractResponse,
)

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

    @router.get(
        "/api/runtime/native-capabilities",
        response_model=NativeCapabilitiesResponse,
    )
    async def get_native_capabilities() -> NativeCapabilitiesResponse:
        """Read-only platform probe; availability never proves isolation (MK-21)."""
        from eventide.native_sandbox.policy import SUPPORT_MATRIX
        from eventide.native_sandbox.probe import detect_native_backends, preferred_backend

        detections = detect_native_backends()
        backends = [
            NativeBackendStatus(
                kind=detection.kind.value,
                available=detection.available,
                binary=str(detection.binary_path) if detection.binary_path else None,
                detail=detection.detail,
                support={
                    capability: level
                    for (kind, capability), level in SUPPORT_MATRIX.items()
                    if kind is detection.kind
                },
            )
            for detection in detections.values()
        ]
        preferred = preferred_backend()
        return NativeCapabilitiesResponse(
            platform=next(iter(detections.values())).platform,
            backends=backends,
            default_backend=preferred[0].value if preferred else None,
            profiles=["read-only", "workspace-write"],
            verification="unverified",
            verification_detail=(
                "检测仅确认平台和可执行文件存在；真实内核越界验证（写/网络/子进程/"
                "符号链接/受保护目录/取消清理）尚未执行（MK-22）。原生执行默认关闭，"
                "启用即为实验特性；后端缺失或策略不支持时明确失败，不回退宿主执行；"
                "文件worker不提供网络或子进程工具；这些权限暂不开放，"
                "平台的子进程限制仍仅部分支持。"
            ),
        )

    return router
