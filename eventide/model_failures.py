"""Unified physical model-attempt failure classification and retry pacing.

Every Host retry decision for a model step flows through this module (MK-08B
slice). Categories are decided from evidence — transport status codes, provider
error payloads and explicit adapter classifications — never by blanket retry
flags. Replay safety is denied whenever a provider executed tools or the
protocol state cannot prove the attempt never produced a complete response.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from eventide.providers.base import ProviderError

# Design default from the MK-08B plan: at most ten physical attempts per model
# step including the first. Not a validated optimum; not a Run/delivery budget.
MODEL_STEP_ATTEMPTS = 10
RETRY_BACKOFF_CAP_SECONDS = 32.0

NETWORK = "network"
TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"
TEMPORARY = "temporary"
STREAM_TRUNCATED = "stream_truncated"
AUTH = "auth"
BILLING = "billing"
REJECTED = "rejected"
PROTOCOL = "protocol"
UNKNOWN = "unknown"

RETRYABLE_CATEGORIES = frozenset(
    {NETWORK, TIMEOUT, RATE_LIMITED, TEMPORARY, STREAM_TRUNCATED},
)


@dataclass(frozen=True, slots=True)
class ModelFailure:
    """One classified physical attempt failure; usage stays None when unknown."""

    category: str
    retryable: bool
    retry_after: float | None = None
    usage: dict[str, int] | None = None
    server_side_activity: bool = False


def retry_delay(
    attempt: int,
    *,
    base: float,
    cap: float = RETRY_BACKOFF_CAP_SECONDS,
    retry_after: float | None = None,
    jitter: Callable[[float], float] | None = None,
) -> float:
    """Exponential backoff 1/2/4/8/16/32s with jitter; Retry-After is a floor."""
    if attempt < 1:
        raise ValueError("Retry delay requires a positive attempt number")
    delay = min(base * (2 ** (attempt - 1)), cap)
    if jitter is not None:
        delay = jitter(delay)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


def default_jitter(delay: float) -> float:
    """Equal upper jitter: the planned delay grows by up to half again."""
    return delay * random.uniform(1.0, 1.5)


def parse_retry_after(value: Any, *, now: float | None = None) -> float | None:
    """Parse a Retry-After header value into seconds; invalid values are None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        seconds = float(text)
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return None
    current = now if now is not None else time.time()
    seconds = parsed.timestamp() - current
    return seconds if seconds > 0 else None


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after_from_exception(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    return parse_retry_after(headers.get("retry-after"))


_BILLING_TEXT = (
    "insufficient balance",
    "insufficient quota",
    "arrears",
    "billing",
    "余额不足",
    "欠费",
)
_AUTH_TEXT = (
    "unauthorized",
    "invalid api key",
    "invalid x-api-key",
    "authentication",
    "forbidden",
    "permission",
)
_RATE_TEXT = ("rate limit", "rate_limit", "too many requests")
_REJECTED_TEXT = ("invalid request", "invalid_request")
_TEMPORARY_TEXT = (
    "overloaded",
    "temporarily unavailable",
    "internal server error",
    "bad gateway",
    "service unavailable",
)
_NETWORK_TEXT = (
    "connection",
    "network",
    "reset by peer",
    "broken pipe",
    "getaddrinfo",
    "name resolution",
    "disconnected",
    "eof",
)
_TIMEOUT_TEXT = ("timed out", "timeout")


def _category_from_status(code: int) -> tuple[str, bool]:
    if code == 408:
        return TIMEOUT, True
    if code == 429:
        return RATE_LIMITED, True
    if code in {401, 403}:
        return AUTH, False
    if code == 402:
        return BILLING, False
    if code in {400, 404, 405, 409, 413, 415, 422}:
        return REJECTED, False
    if 500 <= code <= 599:
        return TEMPORARY, True
    return REJECTED, False


def _category_from_text(text: str) -> tuple[str, bool]:
    lowered = text.lower().replace("_", " ")
    if any(marker in lowered for marker in _BILLING_TEXT):
        return BILLING, False
    if any(marker in lowered for marker in _AUTH_TEXT):
        return AUTH, False
    if any(marker in lowered for marker in _RATE_TEXT):
        return RATE_LIMITED, True
    if any(marker in lowered for marker in _TEMPORARY_TEXT):
        return TEMPORARY, True
    if any(marker in lowered for marker in _TIMEOUT_TEXT):
        return TIMEOUT, True
    if any(marker in lowered for marker in _NETWORK_TEXT):
        return NETWORK, True
    if any(marker in lowered for marker in _REJECTED_TEXT):
        return REJECTED, False
    status = re.search(r"(?:^\s*|\b(?:http|error code|status code)\s*:?\s*)(\d{3})\b", lowered)
    if status:
        return _category_from_status(int(status.group(1)))
    return UNKNOWN, False


def classify_failure(exc: BaseException) -> ModelFailure:
    """Classify one raised failure; unknown and barrier cases never retry."""
    if isinstance(exc, asyncio.CancelledError):
        return ModelFailure(UNKNOWN, False)
    if isinstance(exc, asyncio.TimeoutError):
        return ModelFailure(TIMEOUT, True)
    if isinstance(exc, ProviderError):
        category = exc.category
        if category == UNKNOWN and exc.retryable:
            # Legacy flag-only transport errors from tests and older callers.
            category = NETWORK
        usage = exc.usage if isinstance(exc.usage, dict) and exc.usage else None
        return ModelFailure(
            category,
            bool(exc.retryable)
            and category in RETRYABLE_CATEGORIES
            and not exc.server_side_activity,
            exc.retry_after,
            usage,
            exc.server_side_activity,
        )
    retry_after = _retry_after_from_exception(exc)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            for field in ("code", "type"):
                code = detail.get(field)
                if isinstance(code, str) and _category_from_text(code)[0] == BILLING:
                    return ModelFailure(BILLING, False, retry_after)
    status = _status_code(exc)
    if status is not None:
        category, retryable = _category_from_status(status)
    else:
        category, retryable = _category_from_text(str(exc))
    return ModelFailure(category, retryable, retry_after)


def classify_transport(exc: BaseException, *, started: bool) -> ModelFailure:
    """Classify a raw provider/transport exception observed by an adapter.

    A mid-stream transport drop or a stream that ended without its terminal
    event is reported as ``stream_truncated``: partial output was discarded and
    the retry boundary is the committed model input. An explicit provider error
    event keeps its own category because it carries complete failure evidence.
    """
    failure = classify_failure(exc)
    if started and failure.category in {NETWORK, TIMEOUT, TEMPORARY}:
        return replace(failure, category=STREAM_TRUNCATED)
    return failure


def failure_from_message(message: str, *, started: bool = False) -> ModelFailure:
    """Classify a provider error-event payload that arrived without an exception."""
    return classify_transport(RuntimeError(message), started=started)
