"""Bounded ripgrep backend for regex text search.

The backend is an optional capability: without the ``rg`` binary, regex mode is
declared unavailable instead of degrading to an engine whose match time cannot
be bounded. Each batch runs off the event loop with a wall-clock deadline and
an output byte cap; the process is killed and reaped when either limit trips,
so a pathological pattern can only cost the declared budget. The backend is a
match oracle only — callers verify reported lines against bytes they read
inside the workspace boundary before anything is shown.
"""

import json
import os
import select
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

RG_BINARY = "rg"
DEFAULT_DEADLINE_SECONDS = 10.0
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_STDERR_PREVIEW_CHARS = 300


class RegexBackendUnavailable(RuntimeError):
    """ripgrep was not found; regex mode must not silently degrade to literal."""


def find_backend() -> str | None:
    """Locate the rg binary on PATH without executing anything."""
    return shutil.which(RG_BINARY)


def backend_unavailable_message() -> str:
    return (
        "Error: regex mode is unavailable: the ripgrep backend (rg) was not found on PATH. "
        "Literal mode still works; install ripgrep to enable regex search."
    )


@dataclass(frozen=True)
class ReportedMatch:
    path: str
    line_number: int
    line_text: str
    match_byte_start: int | None = None


@dataclass(frozen=True)
class BatchResult:
    matches: list[ReportedMatch]
    outcome: str  # "completed" | "timeout" | "output_limit" | "backend_error"
    detail: str = ""


def run_regex_batch(
    binary: str,
    pattern: str,
    relative_paths: list[str],
    base: Path,
    *,
    case_sensitive: bool,
    deadline_seconds: float,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> BatchResult:
    """Run one rg batch under a hard deadline and output cap.

    The deadline is enforced with select() on the output pipe so a hung or
    explosive backend is killed after the budget instead of blocking forever;
    stderr is read only after stdout ends, because backend diagnostics are a
    few lines and cannot deadlock a bounded stdout reader.
    """
    argv = [binary, "--json", "--no-messages", "-e", pattern]
    argv.append("-s" if case_sensitive else "-i")
    argv.extend(relative_paths)
    deadline = time.monotonic() + max(deadline_seconds, 0.05)
    try:
        process = subprocess.Popen(
            argv, cwd=base, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return BatchResult([], "backend_error", f"could not start rg: {exc}")
    assert process.stdout is not None and process.stderr is not None
    chunks: list[bytes] = []
    size = 0
    outcome = "completed"
    detail = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outcome = "timeout"
            break
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            outcome = "timeout"
            break
        chunk = os.read(process.stdout.fileno(), 65_536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > max_output_bytes:
            outcome = "output_limit"
            break
    if outcome != "completed":
        process.kill()
    process.wait()
    stderr = process.stderr.read() or b""
    process.stdout.close()
    process.stderr.close()
    if outcome == "completed" and process.returncode not in (0, 1):
        outcome = "backend_error"
        detail = stderr.decode("utf-8", "replace").strip()[:_STDERR_PREVIEW_CHARS]
    return BatchResult(_parse_matches(b"".join(chunks)), outcome, detail)


def _parse_matches(raw: bytes) -> list[ReportedMatch]:
    matches: list[ReportedMatch] = []
    for line in raw.splitlines():
        if b'"type":"match"' not in line and b'"type": "match"' not in line:
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        data = event.get("data") if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        path_field = data.get("path")
        lines_field = data.get("lines")
        number = data.get("line_number")
        if (
            not isinstance(path_field, dict) or not isinstance(lines_field, dict)
            or not isinstance(number, int)
        ):
            continue
        path_text = path_field.get("text")
        line_text = lines_field.get("text")
        if not isinstance(path_text, str) or not isinstance(line_text, str):
            continue  # non-UTF8 path or line text cannot be attributed safely
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        submatches = data.get("submatches")
        byte_start: int | None = None
        if isinstance(submatches, list) and submatches:
            first = submatches[0]
            if isinstance(first, dict) and isinstance(first.get("start"), int):
                byte_start = first["start"]
        matches.append(ReportedMatch(path_text, number, line_text, byte_start))
    return matches
