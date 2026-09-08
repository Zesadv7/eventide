"""Shell execution tool."""

import locale
import subprocess
from pathlib import Path

from nexus_agent.config import WORKDIR


def _console_encoding() -> str:
    """Encoding the local shell uses for its own messages (cp936 on zh-CN Windows)."""
    return locale.getpreferredencoding(False) or "utf-8"


def _decode_output(raw: bytes | None) -> str:
    """Decode child output that mixes UTF-8 tools with the console code page.

    Git, Python and file contents emit UTF-8; cmd built-ins emit the console code
    page. Decoding with a single fixed encoding either raises or mangles the
    other, so try each candidate strictly and fall back to a lossy decode that
    never raises. A missing stream is treated as empty instead of crashing.
    """
    if not raw:
        return ""
    candidates = ["utf-8"]
    console = _console_encoding()
    if console.lower().replace("-", "") != "utf8":
        candidates.append(console)
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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
        output = (_decode_output(result.stdout) + _decode_output(result.stderr)).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
