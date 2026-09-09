"""Shared model/log normalization; UI clipping is independent."""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEY = re.compile(r"(api[_-]?key|authorization|token|secret|password)", re.I)
_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
}


def redact(value: Any, *, max_text: int | None = 4_000) -> Any:
    if isinstance(value, dict):
        limit = (
            200_000
            if max_text is not None and value.get("type") == "provider_state"
            else max_text
        )
        return {
            key: "[REDACTED]"
            if str(key).lower().replace("-", "_") not in _USAGE_KEYS
            and _SECRET_KEY.search(str(key))
            else redact(item, max_text=limit)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, max_text=max_text) for item in value]
    if isinstance(value, str) and max_text is not None and len(value) > max_text:
        return value[:max_text] + f"… [{len(value) - max_text} chars truncated]"
    return value


def normalize(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: normalize(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item, secrets) for item in value]
    return value
