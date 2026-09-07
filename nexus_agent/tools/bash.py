"""Shell execution tool."""

import subprocess
from pathlib import Path

from nexus_agent.config import WORKDIR


def run_bash(command: str, cwd: Path | None = None, run_in_background: bool = False) -> str:
    """Run a shell command and return its output.

    The `run_in_background` flag is consumed by the dispatcher; direct calls
    always execute synchronously.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd or WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
