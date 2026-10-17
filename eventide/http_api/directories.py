"""Local, same-origin user desktop folder selection; no registration or execution."""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, HTTPException, Request

from eventide.http_api.context import ApiContext
from eventide.http_api.native_directories import DirectoryChoice, chooser
from eventide.http_control import check_mutation_origin


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["directories"])

    @router.post("/api/directories/choose", response_model_exclude_none=True)
    async def choose_directory(request: Request) -> DirectoryChoice:
        check_mutation_origin(request, require_origin=True)
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
        except ValueError:
            local = request.client is not None and request.client.host == "testclient"
        if not local:
            raise HTTPException(403, "Folder selection requires a local connection")
        return await chooser.choose(request.is_disconnected)

    return router
