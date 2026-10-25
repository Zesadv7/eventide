"""Provider protocol and factory."""

from __future__ import annotations

from typing import Protocol

from eventide.config import Settings, normalize_provider
from eventide.models import ModelRequest, ModelResponse


class ProviderError(RuntimeError):
    """One provider attempt failure carrying its replay-safety classification.

    ``retryable`` alone never enables a retry: the Host only replays attempts
    whose category is retryable and that carry no provider-side tool activity
    or protocol barrier. ``usage`` keeps tokens the provider already reported
    for a failed attempt; it stays None when usage is unknown.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        category: str = "unknown",
        retry_after: float | None = None,
        usage: dict[str, int] | None = None,
        server_side_activity: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.category = category
        self.retry_after = retry_after
        self.usage: dict[str, int] | None = (
            dict(usage) if isinstance(usage, dict) and usage else None
        )
        self.server_side_activity = server_side_activity


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
