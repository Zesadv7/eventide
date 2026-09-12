"""Best-effort model listing for the composer model picker.

Probes the currently configured provider's listing endpoint without ever
raising for provider failures: the caller gets ``(ids, error)`` and renders an
empty picker with the error note. Requires an API key; without one the result
is an empty list with a note instead of a network call.
"""

from __future__ import annotations

import httpx

_DEFAULT_TIMEOUT = 10.0
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_OPENAI_MODELS_URL = "https://api.openai.com/v1"


class ModelListError(RuntimeError):
    pass


def _endpoint(provider: str, base_url: str | None) -> tuple[str, dict[str, str]]:
    if provider == "anthropic":
        headers = {"x-api-key": "", "anthropic-version": _ANTHROPIC_VERSION}
        return _ANTHROPIC_MODELS_URL, headers
    url = (base_url or _OPENAI_MODELS_URL).rstrip("/") + "/models"
    return url, {}


async def list_models(
    provider: str,
    api_key: str | None,
    base_url: str | None,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[str], str | None]:
    """Return ``(model_ids, error)``. Provider failures never raise."""
    if not api_key:
        return [], "未配置 API Key"
    try:
        url, headers = _endpoint(provider, base_url)
        if provider == "anthropic":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        ids = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id")
            if isinstance(identifier, str):
                ids.append(identifier)
        return ids, None
    except Exception as exc:  # network/HTTP/parse failures degrade to a note
        return [], f"模型列表获取失败：{exc}"
