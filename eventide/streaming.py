"""Optional provider streaming contract; only complete responses contain tool calls."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from eventide.model_failures import classify_transport
from eventide.models import ModelRequest, ModelResponse
from eventide.providers.base import ProviderError

# These are response transport limits, separate from model/Run Token budgets.
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_STREAM_ITEMS = 128
MAX_STREAM_EVENTS = 65_536


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    kind: Literal["text_delta", "response_completed"]
    text: str = ""
    response: ModelResponse | None = None


class StreamingProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...


class StreamLimit:
    """Bound retained text, arguments and private reasoning across a response."""

    def __init__(self) -> None:
        self.size = 0
        self.events = 0

    def event(self) -> None:
        self.events += 1
        if self.events > MAX_STREAM_EVENTS:
            raise ProviderError("Model stream exceeded the event count limit")

    def add(self, value: str) -> str:
        self.size += len(value.encode("utf-8"))
        if self.size > MAX_STREAM_BYTES:
            raise ProviderError("Model stream exceeded the response byte limit")
        return value

    def check_object(self, value: Any) -> None:
        # SDK models expose model_dump; doubles and raw JSON are supported too.
        encoded = json.dumps(to_dict(value), ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > MAX_STREAM_BYTES:
            raise ProviderError("Model stream exceeded the response byte limit")


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def to_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {key: to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(item) for item in value]
    if hasattr(value, "__dict__"):
        return to_dict(vars(value))
    return value


def parse_arguments(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"_invalid_json": value}
    if not isinstance(decoded, dict):
        return {"_invalid_json": value}
    return decoded


def stream_error(
    exc: Exception,
    *,
    started: bool,
    server_side_activity: bool = False,
    usage: dict[str, int] | None = None,
) -> ProviderError:
    """Wrap one adapter failure in the unified classified ProviderError.

    ``started`` marks a stream that already produced events: transport drops
    and missing terminals then become retryable stream truncations, while
    protocol violations and provider-side tool activity stay non-replayable.
    """
    failure = classify_transport(exc, started=started)
    if server_side_activity and not failure.server_side_activity:
        failure = replace(failure, server_side_activity=True, retryable=False)
    if usage and failure.usage is None:
        failure = replace(failure, usage=dict(usage))
    return ProviderError(
        str(exc),
        retryable=failure.retryable,
        category=failure.category,
        retry_after=failure.retry_after,
        usage=failure.usage,
        server_side_activity=failure.server_side_activity,
    )


@asynccontextmanager
async def closing_stream(stream: Any) -> AsyncIterator[Any]:
    """Release the SDK transport on completion, failure, cancellation and aclose."""
    try:
        yield stream
    finally:
        await stream.close()
