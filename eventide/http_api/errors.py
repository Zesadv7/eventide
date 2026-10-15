"""Stable HTTP error envelopes without echoing secrets or request bodies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from eventide.normalization import normalize, redact


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: Any


_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": name.replace("_", " ")}
    for code, name in _ERROR_CODES.items()
}


def error_response(
    status_code: int,
    detail: Any,
    *,
    code: str | None = None,
    secrets: tuple[str, ...] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    safe_detail = normalize(redact(detail), secrets)
    message = safe_detail if isinstance(safe_detail, str) else "Request validation failed"
    return JSONResponse(
        {
            "code": code or _ERROR_CODES.get(status_code, "http_error"),
            "message": message,
            "detail": safe_detail,
        },
        status_code=status_code,
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(
            exc.status_code,
            exc.detail,
            headers=exc.headers,
            secrets=(request.app.state.runtime.settings.api_key or "",),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # Preserve FastAPI's detail list and location/message/type fields. Raw
        # input and validator contexts may contain keys or uploaded file bytes.
        detail = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return error_response(
            422,
            detail,
            secrets=(request.app.state.runtime.settings.api_key or "",),
        )

    @app.exception_handler(Exception)
    async def handle_internal_error(_request: Request, _exc: Exception) -> JSONResponse:
        return error_response(500, "Internal server error")
