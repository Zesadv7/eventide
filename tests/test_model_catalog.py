"""Model listing degrades gracefully: parsed ids or an error note, never a raise."""

import httpx
import pytest

from eventide.model_catalog import list_models


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_openai_compatible_default_endpoint_parses_data_ids():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "gpt-a"}, {"id": "gpt-b"}, {"no": 1}]})

    ids, error = await list_models("openai", "sk-test", None, transport=_mock(handler))
    assert ids == ["gpt-a", "gpt-b"]
    assert error is None
    assert str(requests[0].url) == "https://api.openai.com/v1/models"
    assert requests[0].headers["Authorization"] == "Bearer sk-test"


async def test_custom_base_url_joins_the_models_path():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    ids, error = await list_models(
        "openai", "sk-test", "https://relay.example.com/v1/", transport=_mock(handler)
    )
    assert ids == ["m1"]
    assert error is None
    assert str(requests[0].url) == "https://relay.example.com/v1/models"


async def test_anthropic_sends_api_key_and_version_headers():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "claude-x"}]})

    ids, error = await list_models("anthropic", "sk-ant", None, transport=_mock(handler))
    assert ids == ["claude-x"]
    assert error is None
    assert str(requests[0].url) == "https://api.anthropic.com/v1/models"
    assert requests[0].headers["x-api-key"] == "sk-ant"
    assert requests[0].headers["anthropic-version"] == "2023-06-01"


async def test_http_auth_failure_degrades_to_a_note():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    ids, error = await list_models("openai", "bad", None, transport=_mock(handler))
    assert ids == []
    assert error is not None
    assert "模型列表获取失败" in error
    assert "401" in error


async def test_timeout_degrades_to_a_note():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    ids, error = await list_models("openai", "sk", None, transport=_mock(handler))
    assert ids == []
    assert "模型列表获取失败" in error
    assert "timed out" in error


async def test_non_json_body_degrades_to_a_note():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    ids, error = await list_models("openai", "sk", None, transport=_mock(handler))
    assert ids == []
    assert "模型列表获取失败" in error


@pytest.mark.parametrize(
    "payload",
    [
        [1, 2, 3],
        {"data": "nope"},
        {"data": [{"id": 1}]},  # a non-string id is dropped, not passed on
        {},
    ],
)
async def test_unexpected_payload_shapes_degrade_without_raising(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    ids, error = await list_models("openai", "sk", None, transport=_mock(handler))
    assert ids == []
    assert error is None


async def test_missing_api_key_never_touches_the_network():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a missing key must not produce a request")

    ids, error = await list_models("openai", "", None, transport=_mock(handler))
    assert ids == []
    assert error == "未配置 API Key"
    ids, error = await list_models("anthropic", None, None, transport=_mock(handler))
    assert ids == []
    assert error == "未配置 API Key"
