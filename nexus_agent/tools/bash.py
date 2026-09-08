"""Shell execution tool."""

import subprocess
from pathlib import Path

from nexus_agent.config import WORKDIR
from nexus_agent.utils import decode_output


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
            timeout=120,
        )
        output = (decode_output(result.stdout) + decode_output(result.stderr)).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
