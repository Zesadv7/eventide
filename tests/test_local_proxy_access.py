"""Explicit container forwarding preserves the local administration boundary."""

from dataclasses import replace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.http_api.context import require_local_request
from eventide.providers import ScriptedProvider
from eventide.server import main
from tests.configuration_requests import put_configuration
from tests.test_runtime import settings_for

PEER = "192.168.65.1"
ORIGIN = "http://127.0.0.1:8000"
PROVIDER = {"provider": "anthropic", "model": "test-model", "api_key": "test-secret"}


def client_for(path, peer, peers=()):
    runtime = RuntimeHost(
        replace(settings_for(path), local_proxy_peers=peers), ScriptedProvider([])
    )
    app = create_app(runtime)

    @app.get("/_test/local")
    async def local_read(request: Request):
        require_local_request(request)
        return {"allowed": True}

    return TestClient(app, base_url=ORIGIN, client=(peer, 50000))


@pytest.mark.parametrize("peers,peer", [((), PEER), ((PEER,), "192.168.65.2")])
def test_only_an_explicit_exact_peer_can_manage(isolated_workspace, peers, peer):
    with client_for(isolated_workspace, peer, peers) as client:
        response = put_configuration(client,
            "/api/config/provider", json=PROVIDER, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 403
        assert client.app.state.runtime.provider_config_status()["persistence"] == "environment"


@pytest.mark.parametrize("peer", [PEER, "fd00::1"])
def test_configured_peer_with_local_same_origin_can_manage(isolated_workspace, peer):
    with client_for(isolated_workspace, peer, (peer,)) as client:
        response = put_configuration(client,
            "/api/config/provider", json=PROVIDER, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 200
        assert response.json()["api_key_configured"]
        assert client.get("/_test/local", headers={"Origin": ORIGIN}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://localhost:8000"},
        {"Origin": "http://127.0.0.1:9000"},
        {"Origin": "null"},
        {"Origin": "http://evil.example", "Host": "evil.example"},
        {"Origin": "http://127.0.0.1:invalid"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Origin": ORIGIN + "/some/path"},
    ],
)
def test_extra_peer_requires_local_same_origin_even_for_privileged_reads(
    isolated_workspace, headers
):
    with client_for(isolated_workspace, PEER, (PEER,)) as client:
        assert client.get("/_test/local", headers=headers).status_code == 403
        assert put_configuration(client,
            "/api/config/provider", json=PROVIDER, headers=headers
        ).status_code == 403


def test_forwarding_headers_do_not_grant_peer_access(isolated_workspace):
    with client_for(isolated_workspace, "203.0.113.9", (PEER,)) as client:
        for forwarded in (PEER, "127.0.0.1"):
            headers = {
                "Origin": ORIGIN,
                "X-Forwarded-For": forwarded,
                "Forwarded": f"for={forwarded};host=127.0.0.1:8000;proto=http",
            }
            assert put_configuration(client,
                "/api/config/provider", json=PROVIDER, headers=headers
            ).status_code == 403


def test_loopback_clients_keep_the_existing_no_origin_contract(isolated_workspace):
    with client_for(isolated_workspace, "127.0.0.1") as client:
        assert put_configuration(client,"/api/config/provider", json=PROVIDER).status_code == 200
        assert client.get("/_test/local").status_code == 200


def test_transport_peer_configuration_is_frozen_at_app_creation(isolated_workspace):
    with client_for(isolated_workspace, PEER, (PEER,)) as client:
        runtime = client.app.state.runtime
        runtime.settings = replace(runtime.settings, local_proxy_peers=())
        assert put_configuration(client,
            "/api/config/provider", json=PROVIDER, headers={"Origin": ORIGIN}
        ).status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        "*", "192.168.65.0/24", "host.docker.internal", "0.0.0.0", "::",
        "224.0.0.1", "ff00::1", "fe80::1%en0", "192.168.65.1,", ",192.168.65.1",
        "192.168.65.1,192.168.65.1", "[::1]", "192.168.65.1:8000",
    ],
)
def test_invalid_peer_settings_fail_at_startup(isolated_workspace, monkeypatch, value):
    monkeypatch.setenv("EVENTIDE_LOCAL_PROXY_PEERS", value)
    with pytest.raises(ValueError, match="local_proxy_peers"):
        Settings.from_env(isolated_workspace)


def test_peer_settings_are_normalized_and_default_to_empty(isolated_workspace, monkeypatch):
    monkeypatch.delenv("EVENTIDE_LOCAL_PROXY_PEERS", raising=False)
    assert Settings.from_env(isolated_workspace).local_proxy_peers == ()
    monkeypatch.setenv("EVENTIDE_LOCAL_PROXY_PEERS", " 192.168.65.1, FD00:0000::1 ")
    assert Settings.from_env(isolated_workspace).local_proxy_peers == (PEER, "fd00::1")
    for invalid in (PEER, [PEER], (None,), tuple(f"10.0.0.{i}" for i in range(1, 34))):
        with pytest.raises(ValueError, match="local_proxy_peers"):
            replace(settings_for(isolated_workspace), local_proxy_peers=invalid)


def test_serve_does_not_allow_uvicorn_to_rewrite_socket_peer(isolated_workspace, monkeypatch):
    import uvicorn

    monkeypatch.setattr(Settings, "from_env", lambda: settings_for(isolated_workspace))
    observed = {}

    def serve(app, **options):
        observed.update(options)

    monkeypatch.setattr(uvicorn, "run", serve)
    assert main(["--host", "0.0.0.0"]) == 0
    assert observed["proxy_headers"] is False
