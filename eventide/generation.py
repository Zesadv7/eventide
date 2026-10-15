"""Validated generation patches and endpoint-scoped capability declarations."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class GenerationPatch(BaseModel):
    """Omitted fields inherit; explicit null restores the service default."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_output_tokens: int | None = Field(default=None, gt=0, le=1_000_000)
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None
    ) = None
    thinking: Literal["enabled", "disabled"] | None = None

    def patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class GenerationCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    source: Literal["documented", "declared", "unknown"] = "unknown"
    temperature_max: float | None = Field(default=None, ge=0, le=2)
    top_p: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    thinking: bool = False


def capabilities(provider: str, base_url: str | None, model: str) -> GenerationCapabilities:
    """Recognize exact service/model families, never all compatible endpoints."""
    hostname = urlparse(base_url or "").hostname
    if hostname == "api.deepseek.com" and model in {"deepseek-flash", "deepseek-v4-pro"}:
        return GenerationCapabilities(
            source="documented",
            temperature_max=2,
            top_p=True,
            reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
            thinking=provider == "openai_compatible",
        )
    return GenerationCapabilities()


def resolve_generation(
    layers: list[dict[str, Any]],
    *,
    provider: str,
    base_url: str | None,
    model: str,
    declared: GenerationCapabilities | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for layer in layers:
        values.update(GenerationPatch.model_validate(layer).patch())
    values = {key: value for key, value in values.items() if value is not None}
    supported = declared or capabilities(provider, base_url, model)
    if "temperature" in values:
        if supported.temperature_max is None:
            raise ValueError("Temperature support is unknown; declare model capabilities first")
        if values["temperature"] > supported.temperature_max:
            raise ValueError("Temperature exceeds this model's supported range")
    if "top_p" in values and not supported.top_p:
        raise ValueError("top_p is unsupported or unconfirmed for this model")
    if "reasoning_effort" in values and (
        provider != "openai_responses"
        or values["reasoning_effort"] not in supported.reasoning_efforts
    ):
        raise ValueError("Reasoning effort is unsupported for this model/API")
    if "thinking" in values and (not supported.thinking or provider != "openai_compatible"):
        raise ValueError("Thinking mode selection is unsupported for this model/API")
    deepseek = urlparse(base_url or "").hostname == "api.deepseek.com"
    thinking = values.get("thinking") != "disabled" and values.get("reasoning_effort") != "none"
    if deepseek and thinking and "temperature" in values:
        raise ValueError("DeepSeek temperature has no effect in thinking mode; remove it")
    if deepseek and "top_p" in values:
        if thinking and values["top_p"] < 0.95:
            raise ValueError("DeepSeek thinking top_p must be at least 0.95")
        if not thinking:
            raise ValueError("DeepSeek non-thinking mode fixes top_p; remove the override")
    return values


def wire_parameters(provider: str, generation: dict[str, Any]) -> dict[str, Any]:
    result = {key: generation[key] for key in ("temperature", "top_p") if key in generation}
    if "reasoning_effort" in generation:
        result["reasoning"] = {"effort": generation["reasoning_effort"]}
    if "thinking" in generation:
        result["extra_body"] = {"thinking": {"type": generation["thinking"]}}
    if "max_output_tokens" in generation:
        key = "max_output_tokens" if provider == "openai_responses" else "max_tokens"
        result[key] = generation["max_output_tokens"]
    return result
