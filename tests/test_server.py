"""The deployment entry point starts one service and has no agent subcommands."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from eventide.server import build_parser, main
from tests.test_runtime import settings_for


def test_server_options_and_retired_commands():
    parser = build_parser()
    options = parser.parse_args(["--host", "0.0.0.0", "--port", "9000", "--workspace", "."])
    assert options.host == "0.0.0.0" and options.port == 9000
    assert options.workspace == Path(".")
    for retired in ("chat", "run", "eval", "serve", "rag", "workspace", "deliver"):
        with pytest.raises(SystemExit) as error:
            parser.parse_args([retired])
        assert error.value.code == 2


def test_help_does_not_load_config_or_start_service(monkeypatch, capsys):
    from eventide.config import Settings

    def forbidden():
        raise AssertionError("help must not resolve credentials or create state")

    monkeypatch.setattr(Settings, "from_env", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "eventide-server" in capsys.readouterr().out


def test_deployment_failure_releases_state_ownership(isolated_workspace, monkeypatch, capsys):
    import eventide.server as server
    from eventide.host import RuntimeHost
    from eventide.providers import ScriptedProvider

    settings = settings_for(isolated_workspace)
    monkeypatch.setattr(server.Settings, "from_env", lambda: settings)

    def failed(*_args, **_kwargs):
        raise OSError("port unavailable")

    monkeypatch.setattr(server.uvicorn, "run", failed)
    assert main([]) == 1
    assert "port unavailable" in capsys.readouterr().err
    host = RuntimeHost(settings, ScriptedProvider([]))
    import asyncio

    asyncio.run(host.close())


def test_python_module_help_has_no_key_network_or_files(isolated_workspace):
    source = Path(__file__).resolve().parents[1]
    code = """
import runpy, socket, sys
from pathlib import Path
def denied(*args, **kwargs):
    raise AssertionError('help attempted a network connection')
socket.create_connection = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
sys.argv = ['eventide-server', '--help']
try:
    runpy.run_module('eventide', run_name='__main__')
except SystemExit as error:
    assert error.code == 0
assert list(Path.cwd().iterdir()) == []
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"EVENTIDE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    }
    env.update(PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=isolated_workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "eventide-server" in result.stdout


@pytest.mark.parametrize("port", ["0", "65536"])
def test_invalid_deployment_port_is_rejected_before_host_creation(port):
    with pytest.raises(SystemExit) as error:
        main(["--port", port])
    assert error.value.code == 2
