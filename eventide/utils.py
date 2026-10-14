"""Small utilities shared across the harness."""

import locale


def console_encoding() -> str:
    """Encoding the local shell uses for its own messages (cp936 on zh-CN Windows)."""
    return locale.getpreferredencoding(False) or "utf-8"


def decode_output(raw: bytes | None) -> str:
    """Decode child process output that mixes UTF-8 tools with the console code page.

    Git, Python and file contents emit UTF-8; cmd built-ins emit the console code
    page. Decoding with a single fixed encoding either raises or mangles the
    other, so try each candidate strictly and fall back to a lossy decode that
    never raises. A missing stream is treated as empty instead of crashing.
    """
    if not raw:
        return ""
    candidates = ["utf-8"]
    console = console_encoding()
    if console.lower().replace("-", "") != "utf8":
        candidates.append(console)
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
