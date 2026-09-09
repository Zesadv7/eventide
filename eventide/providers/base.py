"""Provider protocol and factory."""

from __future__ import annotations

from typing import Protocol

from eventide.config import Settings, normalize_provider
from eventide.models import ModelRequest, ModelResponse


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


def build_provider(settings: Settings) -> Provider:
    name = normalize_provider(settings.provider)
    if name == "anthropic":
        from eventide.providers.anthropic import AnthropicProvider

        return AnthropicProvider(settings)
    if name == "openai_compatible":
        from eventide.providers.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(settings)
    if name == "openai_responses":
        from eventide.providers.openai_responses import OpenAIResponsesProvider

        return OpenAIResponsesProvider(settings)
    raise ValueError(f"Unsupported provider '{settings.provider}'")
