"""File read/write/edit/glob tools."""

import hashlib
import os
import re
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

from eventide.policy import resolve_scoped_path

READ_PAGE_CHARS = 10_000
# Headroom for the version header, continuation token and cut footer so a
# paged reply still fits one page budget.
_PAGE_RESERVE_CHARS = 400
MATCH_LOCATION_LIMIT = 5
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PAGE_TOKEN_PATTERN = re.compile(r"^evtfile1:(\d+):(\d+):([0-9a-f]{16}):([0-9a-f]{64})$")

TOKEN_MALFORMED = "Invalid continuation token; re-read the file from offset 0."
TOKEN_FOREIGN = (
    "Continuation token was issued for a different file or working copy; "
    "re-read this file from offset 0."
)
TOKEN_STALE = "File changed since the previous page; re-read it from offset 0."
TOKEN_BEYOND_LINE = (
    "Continuation character offset is beyond that line; re-read the file from offset 0."
)

SEARCH_FILE_BYTES = 1_048_576
SEARCH_SCAN_BYTES = 10 * SEARCH_FILE_BYTES
SEARCH_SCAN_FILES = 2_000


def content_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def edit_match_diagnostics(path: str, old_text: str, text: str) -> str:
    """Bounded explanation when old_text does not match exactly one location."""
    count = text.count(old_text)
    if count == 0:
        return (
            f"old_text not found in {path}; the file is unchanged. Check exact whitespace, "
            "indentation and line endings; CRLF files include the carriage-return characters."
        )
    lines: list[int] = []
    start = text.find(old_text)
    while start != -1 and len(lines) < MATCH_LOCATION_LIMIT:
        lines.append(text.count("\n", 0, start) + 1)
        start = text.find(old_text, start + 1)
    listed = ", ".join(str(number) for number in lines)
    count_label = str(len(lines)) if start == -1 else f"at least {max(count, len(lines) + 1)}"
    more = "" if start == -1 else "; more matches"
    return (
        f"old_text matches {count_label} locations in {path} (lines {listed}{more}); the file is "
        "unchanged. Extend old_text with surrounding context so exactly one location matches."
    )


def version_conflict_message(path: str, current_sha256: str) -> str:
    return (
        f"Content version conflict in {path}: the file changed since it was read (current "
        f"sha256 starts with {current_sha256[:12]}); re-read the file; no change was applied."
    )


def _page_path_id(base: Path, relative_path: str) -> str:
    material = f"evtfile1\x00{base}\x00{relative_path}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def page_token(
    base: Path, relative_path: str, start_line: int, start_char: int, sha: str
) -> str:
    """Self-contained continuation credential binding copy, path and content version.

    The token grants nothing: every read still resolves and re-checks the path,
    and the content hash must match the live file, so a stale or foreign token
    is rejected instead of serving pages across different file states.
    """
    return f"evtfile1:{start_line}:{start_char}:{_page_path_id(base, relative_path)}:{sha}"


def _search_paths(base: Path, pattern: str) -> tuple[list[Path], bool]:
    """Bound directory enumeration as well as reads; never recurse through links."""
    parts = Path(pattern).parts
    prefix = []
    for part in parts:
        if any(char in part for char in "*?["):
            break
        prefix.append(part)
    start = base.joinpath(*prefix)
    if start.is_file():
        return [start], False
    if not start.is_dir():
        return [], False
    pending = [start]
    found: list[Path] = []
    visited = 0
    exhausted = False
    while pending and not exhausted:
        directory = pending.pop()
        entries = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                visited += 1
                if visited > SEARCH_SCAN_FILES:
                    exhausted = True
                    break
                entries.append(entry)
        for entry in sorted(entries, key=lambda item: item.name, reverse=True):
            if entry.is_symlink():
                continue
            if entry.name.startswith(".") and not any(p.startswith(".") for p in parts):
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                relative = path.relative_to(base).parts

                @cache
                def matches(i: int, j: int, relative: tuple[str, ...] = relative) -> bool:
                    if i == len(parts):
                        return j == len(relative)
                    if parts[i] == "**":
                        return matches(i + 1, j) or (
                            j < len(relative) and not relative[j].startswith(".")
                            and matches(i, j + 1)
                        )
                    return (
                        j < len(relative)
                        and (not relative[j].startswith(".") or parts[i].startswith("."))
                        and fnmatchcase(relative[j], parts[i]) and matches(i + 1, j + 1)
                    )

                if matches(0, 0):
                    found.append(path)
    return sorted(found), exhausted


def run_search_text(
    query: str, pattern: str = "**/*", offset: int = 0, limit: int = 50,
    cwd: Path | None = None,
) -> str:
    """Search literal text without an index; offsets count matching source lines."""
    base = (cwd or Path.cwd()).resolve()
    pattern = resolve_scoped_path(base, pattern).relative_to(base).as_posix()
    paths, exhausted = _search_paths(base, pattern)
    page: list[str] = []
    matches = scanned = used_bytes = skipped = used_chars = 0
    more = False
    for path in paths:
        relative = path.relative_to(base).as_posix()
        if scanned >= SEARCH_SCAN_FILES or used_bytes >= SEARCH_SCAN_BYTES:
            exhausted = True
            break
        scanned += 1
        with resolve_scoped_path(base, str(path)).open("rb") as source:
            raw = source.read(min(SEARCH_FILE_BYTES, SEARCH_SCAN_BYTES - used_bytes) + 1)
        used_bytes += len(raw)
        if len(raw) > SEARCH_FILE_BYTES:
            skipped += 1
            continue
        if used_bytes > SEARCH_SCAN_BYTES:
            exhausted = True
            break
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        if "\x00" in text:
            skipped += 1
            continue
        for number, line in enumerate(text.splitlines(), 1):
            position = line.find(query)
            if position < 0:
                continue
            matches += 1
            if matches <= offset:
                continue
            snippet = line[max(0, position - 60):position + max(len(query), 180)]
            item = f"{relative}:{number}: {snippet}"
            if len(page) >= limit or used_chars + len(item) + 1 > 9_000:
                more = True
                break
            page.append(item)
            used_chars += len(item) + 1
        if more:
            break
    status = (
        f"next offset={offset + len(page)}" if more else
        "scan limit reached; narrow pattern (remaining files unsearched)" if exhausted else
        "end of matches"
    )
    header = (
        f"[Literal search; {len(page)} matching lines; {status}; "
        f"enumeration incomplete={exhausted}; "
        f"scanned {scanned} files; skipped {skipped} non-UTF8/binary/oversized files]"
    )
    return header + "\n" + ("\n".join(page) if page else "(no matches in searched text)")


def run_read(
    path: str,
    limit: int | None = None,
    offset: int = 0,
    continuation: str | None = None,
    cwd: Path | None = None,
) -> str:
    """Read a text file, paging long files and reporting the content version.

    Line numbers are one-based source coordinates, including when resuming;
    they are not file text. The header carries the file sha256, which
    edit_file accepts as expected_sha256. Pages cut inside a very long line
    render as ``line:char:`` segments with exact source coordinates, and the
    continuation token binds the working copy, path and content version, so
    pages are never silently stitched across different file states. A plain
    line offset still works for a first read without a token.
    """
    try:
        base = (cwd or Path.cwd()).resolve()
        file_path = resolve_scoped_path(base, path)
        raw = file_path.read_bytes()
        sha = content_sha256(raw)
        text = raw.decode("utf-8")
        relative = file_path.relative_to(base).as_posix()
        lines = text.splitlines()
        total = len(lines)
        if continuation is not None:
            if offset:
                return "Error: pass either continuation or a line offset, not both."
            match = _PAGE_TOKEN_PATTERN.fullmatch(continuation)
            if match is None:
                return f"Error: {TOKEN_MALFORMED}"
            start_line, start_char = int(match.group(1)), int(match.group(2))
            if match.group(3) != _page_path_id(base, relative):
                return f"Error: {TOKEN_FOREIGN}"
            if match.group(4) != sha:
                return f"Error: {TOKEN_STALE}"
            if start_line >= total:
                return (
                    f"[{path} — {total} lines total; sha256={sha}; continuation is past "
                    "the end of the file; re-read from offset 0]"
                )
            if start_char > len(lines[start_line]):
                return f"Error: {TOKEN_BEYOND_LINE}"
        else:
            if not total:
                return f"[{path} — 0 lines total; empty file; sha256={sha}]"
            start_line, start_char = max(int(offset or 0), 0), 0
            if start_line >= total:
                return (
                    f"[{path} — {total} lines total; sha256={sha}; "
                    f"offset {start_line} is past the end]"
                )
        page_limit = None if limit is None else int(limit)
        budget = READ_PAGE_CHARS - _PAGE_RESERVE_CHARS - len(path)
        entries: list[tuple[int, int, str, bool]] = []
        index, char, entered = start_line, start_char, 0
        while index < total and budget >= 2 and (page_limit is None or entered < page_limit):
            rest = lines[index][char:]
            prefix = f"{index + 1}: " if char == 0 else f"{index + 1}:{char}: "
            if len(prefix) + len(rest) + 1 <= budget:
                entries.append((index, char, rest, True))
                budget -= len(prefix) + len(rest) + 1
                index += 1
                char = 0
            else:
                available = budget - len(f"{index + 1}:{char}: ") - 1
                if available <= 0:
                    break
                entries.append((index, char, lines[index][char:char + available], False))
                char += available
                budget = 0
            entered += 1
        if not entries:
            return f"[{path} — {total} lines total; sha256={sha}; no lines returned]"
        body = "\n".join(
            f"{number + 1}: {segment}" if whole and at == 0
            else f"{number + 1}:{at}: {segment}"
            for number, at, segment, whole in entries
        )
        end_of_file = index >= total
        if end_of_file and start_line == 0 and start_char == 0:
            header = f"[{path} — {total} lines total; sha256={sha}]"
        else:
            span = f"showing lines {entries[0][0] + 1}-{entries[-1][0] + 1}"
            if end_of_file:
                header = f"[{path} — {total} lines total; sha256={sha}; {span}; end of file]"
            else:
                token = page_token(base, relative, index, char, sha)
                header = (
                    f"[{path} — {total} lines total; sha256={sha}; {span}; "
                    f"continue with continuation {token}]"
                )
        if end_of_file:
            footer = ""
        elif entries[-1][3]:
            footer = f"... ({total - index} more lines)"
        else:
            footer = f"... (line {entries[-1][0] + 1} continues at char {char})"
        return "\n".join(part for part in (header, body, footer) if part)
    except Exception as exc:
        return f"Error: {exc}"


def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    """Write content to a file, creating parent directories if needed."""
    try:
        base = (cwd or Path.cwd()).resolve()
        file_path = resolve_scoped_path(base, path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(
    path: str,
    old_text: str,
    new_text: str,
    expected_sha256: str | None = None,
    cwd: Path | None = None,
) -> str:
    """Replace the unique occurrence of old_text; ambiguous edits change nothing."""
    try:
        if not old_text:
            return "Error: old_text must not be empty."
        base = (cwd or Path.cwd()).resolve()
        file_path = resolve_scoped_path(base, path)
        raw = file_path.read_bytes()
        if expected_sha256 is not None:
            expected = expected_sha256.lower()
            if not SHA256_PATTERN.fullmatch(expected):
                return "Error: expected_sha256 must be a 64-character lowercase sha256."
            actual = content_sha256(raw)
            if actual != expected:
                return f"Error: {version_conflict_message(path, actual)}"
        text = raw.decode("utf-8")
        first = text.find(old_text)
        if first < 0 or text.find(old_text, first + 1) >= 0:
            return f"Error: {edit_match_diagnostics(path, old_text, text)}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_glob(pattern: str, cwd: Path | None = None) -> str:
    """Return matching paths relative to the working directory."""
    import glob as g

    try:
        base = (cwd or Path.cwd()).resolve()
        results = []
        for match in g.glob(pattern, root_dir=base, recursive=True):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as exc:
        return f"Error: {exc}"
