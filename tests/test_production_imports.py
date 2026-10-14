"""All supported modules import offline without creating Workspace state."""

import os
import subprocess
import sys
from pathlib import Path


def test_production_imports_have_no_network_or_workspace_writes(isolated_workspace):
    source = Path(__file__).resolve().parents[1]
    code = """
import importlib, pkgutil, socket
from pathlib import Path
def denied(*args, **kwargs):
    raise AssertionError('import attempted a network connection')
socket.create_connection = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
import eventide
for module in pkgutil.walk_packages(eventide.__path__, eventide.__name__ + '.'):
    importlib.import_module(module.name)
assert list(Path.cwd().iterdir()) == []
"""
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"EVENTIDE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    }
    env.update(PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=isolated_workspace,
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
