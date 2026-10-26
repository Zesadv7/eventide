"""Validated generation patches and endpoint-scoped capability declarations."""

from __future__ import annotations

from decimal import Decimal
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
    response_format: Literal["text", "json_object"] | None = None

    def patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class GenerationCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    source: Literal["documented", "declared", "unknown"] = "unknown"
    temperature_max: float | None = Field(default=None, ge=0, le=2)
    top_p: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    thinking: bool = False
    thinking_modes: tuple[Literal["enabled", "disabled"], ...] = ("enabled", "disabled")
    top_p_min: float = Field(default=0, ge=0, le=1)
    sampling_precision: int | None = Field(default=None, ge=0, le=6)
    max_output_tokens: int | None = Field(default=None, gt=0, le=1_000_000)
    response_formats: tuple[Literal["text", "json_object"], ...] = ()
    # MK-14: informational capability facts. ``None`` means unknown and is
    # never guessed from a provider name; explicit user declarations stay
    # traceable as ``source="declared"``. They do not gate request validation.
    context_window: int | None = Field(default=None, gt=0, le=100_000_000)
    tools_supported: bool | None = None
    streaming_supported: bool | None = None


def _bigmodel_chat(provider: str, base_url: str | None, model: str) -> bool:
    endpoint = urlparse(base_url or "")
    return (
        provider == "openai_compatible" and endpoint.scheme == "https"
        and endpoint.hostname == "open.bigmodel.cn" and endpoint.port in {None, 443}
        and endpoint.username is None and endpoint.password is None
        and not endpoint.query and not endpoint.fragment
        and endpoint.path.rstrip("/") in {"/api/paas/v4", "/api/coding/paas/v4"}
        and model == "glm-5.3-flash"
    )


def capabilities(provider: str, base_url: str | None, model: str) -> GenerationCapabilities:
    """Recognize exact service/model families, never all compatible endpoints."""
    if _bigmodel_chat(provider, base_url, model):
        return GenerationCapabilities(
            source="documented", temperature_max=1, top_p=True, top_p_min=0.01,
            reasoning_efforts=("low", "high", "max"), thinking=True,
            thinking_modes=("enabled",), sampling_precision=2, max_output_tokens=131_072,
            response_formats=("text", "json_object"),
        )
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


def capability_preset(
    provider: str, base_url: str | None, model: str,
) -> tuple[str, GenerationCapabilities] | None:
    """Return the code-verified preset ``(key, capabilities)`` or ``None``.

    The key names the exact endpoint/model family matched above; capability
    values come only from these in-repo mappings, never from vendor memory.
    """
    caps = capabilities(provider, base_url, model)
    if caps.source != "documented":
        return None
    if _bigmodel_chat(provider, base_url, model):
        return "bigmodel-coding-chat/glm-5.3-flash", caps
    return f"deepseek/{model}", caps


def resolve_generation(
    layers: list[dict[str, Any]],
    *,
    provider: str,
    base_url: str | None,
    model: str,
    declared: GenerationCapabilities | None = None,
    default_max_output_tokens: int | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for layer in layers:
        values.update(GenerationPatch.model_validate(layer).patch())
    values = {key: value for key, value in values.items() if value is not None}
    if default_max_output_tokens is not None:
        values.setdefault("max_output_tokens", default_max_output_tokens)
    supported = declared or capabilities(provider, base_url, model)
    if "response_format" in values and (
        provider != "openai_compatible"
        or values["response_format"] not in supported.response_formats
    ):
        raise ValueError("Response format is unsupported or unconfirmed for this model/API")
    if "temperature" in values:
        if supported.temperature_max is None:
            raise ValueError("Temperature support is unknown; declare model capabilities first")
        if values["temperature"] > supported.temperature_max:
            raise ValueError("Temperature exceeds this model's supported range")
    if "top_p" in values and not supported.top_p:
        raise ValueError("top_p is unsupported or unconfirmed for this model")
    if "reasoning_effort" in values and (
        (provider != "openai_responses" and not _bigmodel_chat(provider, base_url, model))
        or values["reasoning_effort"] not in supported.reasoning_efforts
    ):
        raise ValueError("Reasoning effort is unsupported for this model/API")
    if "thinking" in values and (not supported.thinking or provider != "openai_compatible"):
        raise ValueError("Thinking mode selection is unsupported for this model/API")
    if "thinking" in values and values["thinking"] not in supported.thinking_modes:
        raise ValueError("This model does not support the selected thinking mode")
    if "top_p" in values and values["top_p"] < supported.top_p_min:
        raise ValueError("top_p is below this model's supported range")
    if supported.max_output_tokens is not None and values.get(
        "max_output_tokens", 0
    ) > supported.max_output_tokens:
        raise ValueError("Maximum output exceeds this model's supported range")
    for key in ("temperature", "top_p"):
        if key in values and supported.sampling_precision is not None:
            decimal = Decimal(str(values[key]))
            if decimal != decimal.quantize(Decimal(1).scaleb(-supported.sampling_precision)):
                raise ValueError(f"{key} supports at most {supported.sampling_precision} decimals")
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
    if "response_format" in generation:
        if provider != "openai_compatible":
            raise ValueError("Response format is unsupported for this API")
        result["response_format"] = {"type": generation["response_format"]}
    if "reasoning_effort" in generation:
        if provider == "openai_compatible":
            result["reasoning_effort"] = generation["reasoning_effort"]
        else:
            result["reasoning"] = {"effort": generation["reasoning_effort"]}
    if "thinking" in generation:
        result["extra_body"] = {"thinking": {"type": generation["thinking"]}}
    if "max_output_tokens" in generation:
        key = "max_output_tokens" if provider == "openai_responses" else "max_tokens"
        result[key] = generation["max_output_tokens"]
    return result
