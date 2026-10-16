"""The configured request deadline also governs SDK transport phases."""

import pytest

from eventide.providers.anthropic import AnthropicProvider
from eventide.providers.openai_compatible import OpenAICompatibleProvider
from eventide.providers.openai_responses import OpenAIResponsesProvider
from tests.test_runtime import settings_for


@pytest.mark.parametrize(
    "provider_class", [AnthropicProvider, OpenAICompatibleProvider, OpenAIResponsesProvider]
)
@pytest.mark.asyncio
async def test_sdk_transport_uses_configured_model_timeout(tmp_path, provider_class):
    provider = provider_class(settings_for(tmp_path, api_key="fake", model_timeout=87.5))
    try:
        client = provider._client
        assert client.max_retries == 0
        assert client.timeout == 87.5
        transport_timeout = client._client.timeout
        assert transport_timeout.connect == 87.5
        assert transport_timeout.read == 87.5
        assert transport_timeout.write == 87.5
        assert transport_timeout.pool == 87.5
    finally:
        await provider.close()
