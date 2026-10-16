"""Exact GLM endpoint constraints and real SDK serialization without external calls."""

import pytest

from eventide.generation import capabilities, resolve_generation

URL = "https://open.bigmodel.cn/api/coding/paas/v4/"
OPTIONS = dict(provider="openai_compatible", base_url=URL, model="glm-5.3-flash")


@pytest.mark.parametrize("url", [URL, "https://open.bigmodel.cn/api/paas/v4"])
def test_exact_glm_chat_capabilities_and_zero(url):
    support = capabilities("openai_compatible", url, "glm-5.3-flash")
    assert support.source == "documented"
    assert support.thinking_modes == ("enabled",)
    assert support.reasoning_efforts == ("low", "high", "max")
    assert resolve_generation([
        {"temperature": 0, "top_p": 0.95, "reasoning_effort": "low", "thinking": "enabled"},
    ], **{**OPTIONS, "base_url": url})["temperature"] == 0


@pytest.mark.parametrize("change", [
    {"base_url": "https://open.bigmodel.cn.invalid/api/coding/paas/v4"},
    {"base_url": "https://open.bigmodel.cn/other"},
    {"base_url": "https://open.bigmodel.cn/api/v1"},
    {"base_url": "http://open.bigmodel.cn/api/coding/paas/v4"},
    {"base_url": URL + "?proxy=1"},
    {"model": "glm-5.3-flash-future"},
    {"provider": "openai_responses"},
])
def test_no_glm_capability_guessing(change):
    assert capabilities(**{**OPTIONS, **change}).source == "unknown"


@pytest.mark.parametrize("patch", [
    {"temperature": 1.01}, {"temperature": 0.555}, {"top_p": 0.001},
    {"top_p": 0.951}, {"reasoning_effort": "medium"}, {"thinking": "disabled"},
    {"max_output_tokens": 131_073},
])
def test_invalid_glm_configuration_rejected_before_request(patch):
    with pytest.raises(ValueError):
        resolve_generation([patch], **OPTIONS)


def test_glm_server_default_and_null_reset_must_also_fit_model():
    with pytest.raises(ValueError, match="Maximum output"):
        resolve_generation([{"max_output_tokens": None}], default_max_output_tokens=140_000,
                           **OPTIONS)
    assert resolve_generation([{"max_output_tokens": 131_072}], **OPTIONS)[
        "max_output_tokens"
    ] == 131_072
