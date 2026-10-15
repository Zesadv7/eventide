"""Per-application transport state; execution facts remain in RuntimeHost."""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

from eventide.host import RuntimeHost
from eventide.models import ToolCall


class ApprovalBroker:
    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[bool]] = {}

    async def request(self, approval_id: str, _call: ToolCall, _reason: str) -> bool:
        future = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = future
        try:
            return await future
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool) -> bool:
        future = self.pending.get(approval_id)
        if not future or future.done():
            return False
        future.set_result(approved)
        return True


def require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host == "testclient":
        return
    try:
        is_local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_local = host.lower() == "localhost"
    if not is_local:
        raise HTTPException(status_code=403, detail="模型凭据只能从运行服务的本机管理")


@dataclass(slots=True)
class ApiContext:
    runtime: RuntimeHost
    approvals: ApprovalBroker = field(default_factory=ApprovalBroker)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    reserved: set[str] = field(default_factory=set)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        try:
            return self.runtime.session_status(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        try:
            return self.runtime.workspace_status(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
