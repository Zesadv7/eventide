"""Wire-level tests: assert the actual HTTP body each adapter sends.

The empty-`tools` regression bit on the serialized request body (strict
OpenAI-compatible backends reject ``"tools": []``), so a kwargs-level mock is
not enough — these tests capture the real JSON body on the wire.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexus_agent.config import Settings
from nexus_agent.models import ModelRequest
from nexus_agent.providers.openai_compatible import OpenAICompatibleProvider

MINIMAL_COMPLETION = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _capture_server(captured: dict) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured["path"] = self.path
            captured["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = json.dumps(MINIMAL_COMPLETION).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def openai_wire(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    captured: dict = {}
    server = _capture_server(captured)
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", captured
    finally:
        server.shutdown()
        server.server_close()


def _settings(base_url, isolated_workspace):
    return Settings(
        workdir=isolated_workspace,
        state_dir=isolated_workspace / ".nexus",
        provider="openai_compatible",
        api_key="test-key",
        base_url=base_url,
        model="test-model",
        fallback_model=None,
    )


def _request(tools=()):
    return ModelRequest("system", [{"role": "user", "content": "hi"}], list(tools), "model", 32)


async def test_openai_wire_omits_tools_when_empty(openai_wire, isolated_workspace):
    base_url, captured = openai_wire
    provider = OpenAICompatibleProvider(_settings(base_url, isolated_workspace))
    await provider.complete(_request())
    await provider.close()
    assert captured["path"] == "/v1/chat/completions"
    assert "tools" not in captured["body"]


async def test_openai_wire_includes_tools_when_present(openai_wire, isolated_workspace):
    base_url, captured = openai_wire
    provider = OpenAICompatibleProvider(_settings(base_url, isolated_workspace))
    tools = [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}]
    await provider.complete(_request(tools))
    await provider.close()
    assert captured["body"]["tools"][0]["function"]["name"] == "read_file"
