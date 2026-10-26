"""Versioned model capability catalog (MK-14).

A capability record binds Provider + protocol + canonical endpoint + model to
declared capabilities with an explicit source and version. Three states stay
separate and never substitute for each other:

- ``config_saved``: a provider configuration is stored for this endpoint.
- ``connection``: a real connection probe Run reached a terminal outcome.
- ``tool_verification``: real tool calls were verified against the endpoint.
  Nothing in this module sets it to verified; a passed connection probe is
  not tool verification.

Capability values come only from the code presets in
:mod:`eventide.generation` or from explicit user declarations. Unknown facts
stay ``None`` and are never guessed from provider names. No automatic
provider routing is added here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from eventide.generation import GenerationCapabilities, capability_preset
from eventide.workspace import digest

CATALOG_VERSION = "1"

#: Preset sources point at the in-repo mapping. A historical successful call
#: does not verify every declared capability, so no blanket verification date
#: is inferred from earlier experiments.
PRESET_SOURCES: dict[str, dict[str, str | None]] = {
    "bigmodel-coding-chat/glm-5.3-flash": {
        "reference": "eventide/generation.py capabilities() BigModel chat 映射",
        "verified_at": None,
        "documented_url": None,
    },
    "deepseek/deepseek-flash": {
        "reference": "eventide/generation.py capabilities() DeepSeek 映射",
        "verified_at": None,
        "documented_url": None,
    },
    "deepseek/deepseek-v4-pro": {
        "reference": "eventide/generation.py capabilities() DeepSeek 映射",
        "verified_at": None,
        "documented_url": None,
    },
}

_DEFAULT_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com",
    "openai_compatible": "https://api.openai.com/v1",
    "openai_responses": "https://api.openai.com/v1",
}

_PROTOCOLS = {
    "anthropic": "anthropic_messages",
    "openai_compatible": "openai_chat",
    "openai_responses": "openai_responses",
}


def canonical_endpoint(provider: str, base_url: str | None) -> str:
    """Normalize an endpoint to its canonical form for catalog identity.

    Trailing slashes and explicit default ports collapse; different paths or
    hosts stay distinct, so the same model name on another endpoint is a
    different identity and never inherits capabilities.
    """
    raw = (base_url or "").strip() or _DEFAULT_ENDPOINTS.get(provider, "")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return raw.rstrip("/")
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if port is not None and port != default:
        host = f"{host}:{port}"
    authority = (parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else "") + host
    return parsed._replace(netloc=authority, path=parsed.path.rstrip("/"), fragment="").geturl()


def endpoint_identity(provider: str, base_url: str | None, model: str) -> dict[str, Any]:
    """MK-14 identity: provider + protocol + canonical endpoint + model."""
    canonical = canonical_endpoint(provider, base_url)
    parsed = urlparse(canonical)
    # Keep routing/authentication distinctions in the identity hash, never in
    # the public display. Query parameters can contain credentials.
    display = parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment="",
    ).geturl()
    return {
        "provider": provider,
        "protocol": _PROTOCOLS.get(provider, provider),
        "canonical_endpoint": display,
        "model": model,
        "identity_hash": digest([provider, _PROTOCOLS.get(provider, provider), canonical, model]),
    }


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """Immutable projection returned to API/UI; ``to_dict`` shapes the wire."""

    identity: dict[str, Any]
    source: str
    catalog_version: str
    capabilities: dict[str, Any]
    preset: dict[str, Any] | None
    overrides: dict[str, Any]
    verification: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "source": self.source,
            "catalog_version": self.catalog_version,
            "capabilities": self.capabilities,
            "preset": self.preset,
            "overrides": self.overrides,
            "verification": self.verification,
        }


def _overrides(
    declared: GenerationCapabilities, preset: GenerationCapabilities | None,
) -> dict[str, Any]:
    """Fields the user declaration sets differently from the code preset."""
    if preset is None:
        return {"fields": [], "note": "无代码预设；整份能力来自用户声明"}
    base = preset.model_dump()
    changed = [
        key
        for key, value in declared.model_dump().items()
        if key != "source" and value != base[key]
    ]
    return {"fields": changed, "note": "用户声明覆盖以下预设字段" if changed else ""}


def capability_record(
    *,
    provider: str,
    base_url: str | None,
    model: str,
    declared: dict[str, Any] | None,
    model_identity: str,
    config_saved: bool,
) -> CapabilityRecord:
    """Assemble the catalog record from config facts only (no event scans).

    ``declared`` is the saved user capability declaration for this exact
    endpoint/model identity, when one exists. ``config_saved`` reports whether
    a stored provider configuration resolves to this same endpoint identity.
    """
    identity = endpoint_identity(provider, base_url, model)
    preset_match = capability_preset(provider, base_url, model)
    preset_caps = preset_match[1] if preset_match else None
    if declared:
        caps = GenerationCapabilities.model_validate(declared)
        source = "declared"
    elif preset_caps is not None:
        caps = preset_caps
        source = "documented"
    else:
        caps = GenerationCapabilities()
        source = "unknown"
    preset_info = None
    if preset_match is not None:
        preset_info = {
            "key": preset_match[0],
            **PRESET_SOURCES.get(preset_match[0], {"reference": None, "verified_at": None}),
        }
    overrides = (
        _overrides(caps, preset_caps)
        if source == "declared"
        else {"fields": [], "note": ""}
    )
    if source == "declared":
        overrides["declaration_ref"] = model_identity
    return CapabilityRecord(
        identity=identity,
        source=source,
        catalog_version=CATALOG_VERSION,
        capabilities=caps.model_dump(),
        preset=preset_info,
        overrides=overrides,
        verification={
            "config_saved": config_saved,
            "connection": {
                "status": "never_probed",
                "checked_at": None,
                "run_id": None,
                "error_type": None,
            },
            "tool_verification": {"status": "not_verified", "note": "连接通过不代表工具验证通过"},
        },
    )


def connection_verification(
    store: Any, provider: str, base_url: str | None, model: str,
) -> dict[str, Any]:
    """Derive the latest probe outcome for this exact endpoint identity.

    Probe Runs record ``endpoint_hash`` over the raw configured base URL, so
    matching is byte-exact: the same model on another endpoint is never
    treated as verified here.
    """
    target = digest(base_url)
    run_ids = [
        event["run_id"]
        for event in store.facts_by_type(("run.started",))
        if event["payload"].get("source") == "connection_probe"
        and event["payload"].get("provider") == provider
        and event["payload"].get("endpoint_hash") == target
        and event["payload"].get("model") == model
    ]
    result: dict[str, Any] = {
        "status": "never_probed", "checked_at": None, "run_id": None, "error_type": None,
    }
    for run_id in run_ids:
        for event in store.run_events(run_id):
            if event["type"] not in {"run.completed", "run.failed", "run.interrupted"}:
                continue
            payload = event["payload"]
            status = {
                "run.completed": "passed",
                "run.failed": "failed",
                # A cancelled/timed-out probe has no confirmed outcome; it is
                # neither a passed nor a failed connection check.
                "run.interrupted": "unknown",
            }[event["type"]]
            result = {
                "status": status,
                "checked_at": datetime.fromtimestamp(event["ts"], timezone.utc).isoformat(),
                "run_id": run_id,
                "error_type": payload.get("reason") if event["type"] == "run.failed" else None,
            }
    return result
