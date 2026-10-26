"""File read/write/edit/glob tools."""

import hashlib
import os
import re
import time
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path
from typing import Any

from eventide.models import ToolOutput
from eventide.policy import resolve_scoped_path
from eventide.tools import regex_backend

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
SEARCH_REGEX_BATCH_FILES = 256
SEARCH_REGEX_DEADLINE_SECONDS = 10.0
SEARCH_LINE_DISPLAY_CHARS = 240
SEARCH_LINE_CONTEXT_CHARS = 60


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


class _PageBuilder:
    """Render hits and merged context blocks under one page budget.

    Context ranges of hits inside a file are merged before rendering, so a
    line that is both a hit and somebody's context appears once, as a hit.
    """

    def __init__(self, offset: int, limit: int, context_lines: int):
        self.offset_remaining = offset
        self.limit = limit
        self.context_lines = context_lines
        self.budget = 9_000
        self.page: list[str] = []
        self.sources: list[dict[str, Any]] = []
        self.hits_shown = 0
        self.files_shown = 0
        self.more = False

    def add_file(
        self, relative: str, sha: str, lines: list[str],
        hits: list[tuple[int, int | None]],
    ) -> None:
        """Add one file's hits (line number, match char position) to the page."""
        if self.more:
            return
        if self.offset_remaining >= len(hits):
            self.offset_remaining -= len(hits)
            return
        if self.offset_remaining:
            hits = hits[self.offset_remaining:]
            self.offset_remaining = 0
        if not hits:
            return
        positions = dict(hits)
        hit_numbers = {number for number, _ in hits}
        ranges: list[list[int]] = []
        for number, _ in hits:
            start = max(1, number - self.context_lines)
            end = min(len(lines), number + self.context_lines)
            if ranges and start <= ranges[-1][1] + 1:
                ranges[-1][1] = max(ranges[-1][1], end)
            else:
                ranges.append([start, end])
        header = f"{relative} sha256={sha}"
        header_cost = len(header) + 1
        if self.page and self.budget < header_cost:
            self.more = True
            return
        rendered_header = False
        source: dict[str, Any] = {"path": relative, "sha256": sha, "lines": []}
        for start, end in ranges:
            for number in range(start, end + 1):
                is_hit = number in hit_numbers
                if is_hit and self.hits_shown >= self.limit:
                    self.more = True
                    return
                text = lines[number - 1]
                position = positions[number] if is_hit else None
                segment = _clip_line(text, position)
                entry = f"{relative}:{number}: {segment}" if is_hit else (
                    f"{relative}:{number}- {segment}"
                )
                cost = len(entry) + 1 + (0 if rendered_header else header_cost)
                if self.budget < cost:
                    self.more = True
                    return
                if not rendered_header:
                    self.page.append(header)
                    self.budget -= header_cost
                    rendered_header = True
                    self.files_shown += 1
                    self.sources.append(source)
                self.page.append(entry)
                source["lines"].append({"line": number, "match": is_hit, "text": segment})
                self.budget -= len(entry) + 1
                if is_hit:
                    self.hits_shown += 1


def _clip_line(text: str, position: int | None) -> str:
    """Bound one source line for display, marking exactly what was elided."""
    start = max(0, (position if position is not None else 0) - SEARCH_LINE_CONTEXT_CHARS)
    segment = text[start:start + SEARCH_LINE_DISPLAY_CHARS]
    prefix = "…" if start else ""
    elided = len(text) - start - len(segment)
    suffix = f"… (+{elided} chars)" if elided > 0 else ""
    return prefix + segment + suffix


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


def _literal_line_matcher(query: str, case_sensitive: bool) -> re.Pattern[str] | None:
    """Case-insensitive literal matching via an escaped pattern.

    re.escape yields a pure literal alternation with no backtracking states, so
    matching stays linear; case folding is left to the engine because
    str.casefold can change string length and corrupt match positions.
    """
    if case_sensitive:
        return None
    return re.compile(re.escape(query), re.IGNORECASE)


def _read_search_file(base: Path, path: Path, used_bytes: int) -> tuple[bytes, bool]:
    """Read one candidate inside the file and scan byte budgets.

    Returns the bytes and whether the scan cap cut the file short; such a file
    was searched only partially and must not be reported as fully covered.
    Oversized files are returned as-is for the caller's skip rules.
    """
    remaining = SEARCH_SCAN_BYTES - used_bytes
    want = min(SEARCH_FILE_BYTES, remaining) + 1
    with resolve_scoped_path(base, str(path)).open("rb") as source:
        raw = source.read(want)
    return raw, len(raw) == want and remaining < SEARCH_FILE_BYTES


def run_search_text(
    query: str, pattern: str = "**/*", offset: int = 0, limit: int = 50,
    mode: str = "literal", case_sensitive: bool = True, context_lines: int = 0,
    cwd: Path | None = None,
) -> str:
    """Text-only developer interface for the same bounded search result."""
    output = search_text_output(
        query, pattern, offset, limit, mode, case_sensitive, context_lines, cwd,
    )
    return output.content if isinstance(output, ToolOutput) else output


def search_text_output(
    query: str, pattern: str = "**/*", offset: int = 0, limit: int = 50,
    mode: str = "literal", case_sensitive: bool = True, context_lines: int = 0,
    cwd: Path | None = None,
) -> str | ToolOutput:
    """Search workspace files without an index, with versioned source evidence.

    Line numbers are one-based source coordinates; hit lines render as
    ``path:line:`` and context lines as ``path:line-``. Every source with
    displayed lines carries the sha256 of the exact bytes that were searched,
    the same content version read_file reports and edit_file accepts. Literal
    mode is pure Python and always available; regex mode requires the local
    ripgrep backend and reports an error when it is missing instead of
    treating the pattern as literal. Reported regex matches are verified line
    by line against bytes read inside the workspace boundary, so backend
    output can never introduce text from outside it. Scan, skip, timeout and
    verification counts are explicit: an empty result only covers the files
    actually searched, never the whole workspace.
    """
    if mode not in {"literal", "regex"}:
        return f"Error: mode must be 'literal' or 'regex', got {mode!r}."
    if not 0 <= int(context_lines) <= 10:
        return "Error: context_lines must be between 0 and 10."
    context_lines = int(context_lines)
    rg_binary = ""
    matcher: re.Pattern[str] | None = None
    if mode == "literal":
        matcher = _literal_line_matcher(query, case_sensitive)
    else:
        found = regex_backend.find_backend()
        if found is None:
            return regex_backend.backend_unavailable_message()
        rg_binary = found
    try:
        base = (cwd or Path.cwd()).resolve()
        pattern = resolve_scoped_path(base, pattern).relative_to(base).as_posix()
        paths, exhausted = _search_paths(base, pattern)
    except PermissionError:
        raise
    except Exception as exc:
        return f"Error: {exc}"
    builder = _PageBuilder(offset, limit, context_lines)
    scanned = skipped = unverified = 0
    used_bytes = 0
    timed_out = output_capped = False
    backend_error = ""
    try:
        if mode == "literal":
            for path in paths:
                if scanned >= SEARCH_SCAN_FILES or used_bytes >= SEARCH_SCAN_BYTES:
                    exhausted = True
                    break
                scanned += 1
                raw, scan_capped = _read_search_file(base, path, used_bytes)
                used_bytes += len(raw)
                if len(raw) > SEARCH_FILE_BYTES:
                    skipped += 1
                    continue
                if scan_capped or used_bytes > SEARCH_SCAN_BYTES:
                    exhausted = True
                    skipped += 1
                    break
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                if "\x00" in text:
                    skipped += 1
                    continue
                lines = text.splitlines()
                hits = _literal_hits(lines, query, matcher)
                if hits:
                    builder.add_file(
                        path.relative_to(base).as_posix(), content_sha256(raw), lines, hits,
                    )
                    if builder.more:
                        break
        else:
            candidates: list[tuple[str, bytes]] = []
            for path in paths:
                if scanned >= SEARCH_SCAN_FILES or used_bytes >= SEARCH_SCAN_BYTES:
                    exhausted = True
                    break
                scanned += 1
                relative = path.relative_to(base).as_posix()
                try:
                    relative.encode("utf-8")
                except UnicodeEncodeError:
                    skipped += 1
                    continue
                raw, scan_capped = _read_search_file(base, path, used_bytes)
                used_bytes += len(raw)
                if len(raw) > SEARCH_FILE_BYTES:
                    skipped += 1
                    continue
                if scan_capped or used_bytes > SEARCH_SCAN_BYTES:
                    exhausted = True
                    skipped += 1
                    break
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                if b"\x00" in raw:
                    skipped += 1
                    continue
                candidates.append((relative, raw))
            started = time.monotonic()
            for batch_start in range(0, len(candidates), SEARCH_REGEX_BATCH_FILES):
                if builder.more:
                    break
                remaining = SEARCH_REGEX_DEADLINE_SECONDS - (time.monotonic() - started)
                if remaining <= 0.05:
                    timed_out = True
                    exhausted = True
                    break
                batch = candidates[batch_start:batch_start + SEARCH_REGEX_BATCH_FILES]
                result = regex_backend.run_regex_batch(
                    rg_binary, query, [relative for relative, _ in batch], base,
                    case_sensitive=case_sensitive, deadline_seconds=remaining,
                    max_output_bytes=regex_backend.MAX_OUTPUT_BYTES,
                )
                if result.outcome == "backend_error":
                    backend_error = result.detail
                    exhausted = True
                    break
                if result.outcome == "timeout":
                    timed_out = True
                    exhausted = True
                    break
                if result.outcome == "output_limit":
                    output_capped = True
                    exhausted = True
                    break
                raw_by_path = dict(batch)
                grouped: dict[str, list[regex_backend.ReportedMatch]] = {}
                for match in result.matches:
                    grouped.setdefault(match.path, []).append(match)
                for relative, raw in raw_by_path.items():
                    reported = grouped.get(relative)
                    if not reported:
                        continue
                    verified = _verify_regex_matches(raw, reported)
                    if verified is None:
                        unverified += 1
                        continue
                    lines = raw.decode("utf-8").split("\n")
                    builder.add_file(relative, content_sha256(raw), lines, verified)
                    if builder.more:
                        break
            if backend_error and not builder.page:
                return f"Error: regex backend rejected the search: {backend_error[:300]}"
    except PermissionError:
        raise
    except Exception as exc:
        return f"Error: {exc}"
    notes = ""
    if timed_out:
        notes += (
            f"; regex deadline exceeded after {SEARCH_REGEX_DEADLINE_SECONDS:.0f}s; "
            "remaining files unsearched"
        )
    if output_capped:
        notes += "; regex output cap reached; matches may be missed"
    if backend_error and builder.page:
        notes += "; regex backend error after partial results; remaining files unsearched"
    if unverified:
        notes += f"; unverified {unverified} changed files excluded"
    if builder.more:
        status = f"next offset={offset + builder.hits_shown}"
    elif exhausted and not (timed_out or output_capped):
        status = "scan limit reached; narrow pattern (remaining files unsearched)"
    elif exhausted:
        status = "remaining files unsearched"
    else:
        status = "end of matches"
    backend_label = "python" if mode == "literal" else "ripgrep"
    header = (
        f"[Text search mode={mode} backend={backend_label} "
        f"case={'sensitive' if case_sensitive else 'insensitive'} "
        f"context={context_lines}; {builder.hits_shown} matching lines in "
        f"{builder.files_shown} files; {status}; enumeration incomplete={exhausted}; "
        f"scanned {scanned} files; skipped {skipped} "
        f"non-UTF8/binary/oversized/unsearched files{notes}; "
        'hits use "path:line:"; context lines use "path:line-"; '
        "sha256 values are the exact searched content, accepted by edit_file "
        "expected_sha256]"
    )
    if not builder.page:
        if skipped or exhausted or unverified or timed_out or output_capped:
            body = (
                "(no matches in the files searched; skipped, unsearched or excluded "
                "files above mean this is not a whole-repo empty result)"
            )
        else:
            body = "(no matches in searched text)"
    else:
        body = "\n".join(builder.page)
    return ToolOutput(header + "\n" + body, metadata={"source_evidence": {
        "kind": "search", "mode": mode, "case_sensitive": case_sensitive,
        "context_lines": context_lines, "scanned": scanned, "skipped": skipped,
        "unverified": unverified, "timed_out": timed_out, "output_capped": output_capped,
        "partial": bool(exhausted or skipped or unverified or backend_error),
        "next_offset": offset + builder.hits_shown if builder.more else None,
        "sources": builder.sources,
    }})


def _literal_hits(
    lines: list[str], query: str, matcher: re.Pattern[str] | None,
) -> list[tuple[int, int | None]]:
    hits: list[tuple[int, int | None]] = []
    for number, line in enumerate(lines, 1):
        if matcher is not None:
            found = matcher.search(line)
            position = found.start() if found else -1
        else:
            position = line.find(query)
        if position >= 0:
            hits.append((number, position))
    return hits


def _verify_regex_matches(
    raw: bytes, reported: list[regex_backend.ReportedMatch],
) -> list[tuple[int, int | None]] | None:
    """Confirm reported matches against the bytes actually read, or reject all.

    A reported line must be byte-identical to the same numbered line of the
    read content; regex matching is a pure function of the line text, so equal
    text proves the match exists in the hashed content. Any disagreement
    (including a changed or replaced file) rejects the whole source instead of
    mixing evidence across content versions.
    """
    raw_lines = raw.split(b"\n")
    seen: set[int] = set()
    verified: list[tuple[int, int | None]] = []
    for match in reported:
        if not 1 <= match.line_number <= len(raw_lines) or match.line_number in seen:
            return None
        line_bytes = raw_lines[match.line_number - 1]
        try:
            if line_bytes.decode("utf-8") != match.line_text:
                return None
            position = (
                len(line_bytes[:match.match_byte_start].decode("utf-8"))
                if match.match_byte_start is not None and match.match_byte_start <= len(line_bytes)
                else None
            )
        except UnicodeDecodeError:
            return None
        seen.add(match.line_number)
        verified.append((match.line_number, position))
    return verified


def run_read(
    path: str, limit: int | None = None, offset: int = 0,
    continuation: str | None = None, cwd: Path | None = None,
) -> str:
    """Text-only developer interface; production also records structured evidence."""
    return read_file_output(path, limit, offset, continuation, cwd).content


def read_file_output(
    path: str,
    limit: int | None = None,
    offset: int = 0,
    continuation: str | None = None,
    cwd: Path | None = None,
) -> ToolOutput:
    """Read a text file, paging long files and reporting the content version.

    Line numbers are one-based source coordinates, including when resuming;
    they are not file text. The header carries the file sha256, which
    edit_file accepts as expected_sha256. Pages cut inside a very long line
    render as ``line:char:`` segments with exact source coordinates, and the
    continuation token binds the working copy, path and content version, so
    pages are never silently stitched across different file states. A plain
    line offset still works for a first read without a token.
    """
    base = (cwd or Path.cwd()).resolve()
    try:
        file_path = resolve_scoped_path(base, path)
        raw = file_path.read_bytes()
    except (OSError, ValueError) as exc:
        return ToolOutput(f"Error: {exc}", True, {
            "source_evidence": {"kind": "file", "path": path, "status": "error"},
            "code": "read_failed",
        })
    return render_read_file(raw, base, file_path.relative_to(base).as_posix(), path,
                            limit=limit, offset=offset, continuation=continuation)


def render_read_file(
    raw: bytes, base: Path, relative: str, path: str, *,
    limit: int | None = None, offset: int = 0, continuation: str | None = None,
) -> ToolOutput:
    """Render the same version-bound page from bytes read by any file backend."""
    evidence: dict[str, Any] = {"kind": "file", "path": path}

    def result(content: str, code: str | None = None, **page: Any) -> ToolOutput:
        return ToolOutput(content, code is not None, {
            "source_evidence": {**evidence, **page, "status": "error" if code else "read"},
            **({"code": code} if code else {}),
        })

    try:
        sha = content_sha256(raw)
        text = raw.decode("utf-8")
        lines = text.splitlines()
        total = len(lines)
        evidence.update(path=relative, sha256=sha, total_lines=total)
        if continuation is not None:
            if offset:
                return result(
                    "Error: pass either continuation or a line offset, not both.", "invalid_page",
                )
            match = _PAGE_TOKEN_PATTERN.fullmatch(continuation)
            if match is None:
                return result(f"Error: {TOKEN_MALFORMED}", "invalid_page")
            start_line, start_char = int(match.group(1)), int(match.group(2))
            if match.group(3) != _page_path_id(base, relative):
                return result(f"Error: {TOKEN_FOREIGN}", "foreign_page")
            if match.group(4) != sha:
                return result(f"Error: {TOKEN_STALE}", "stale_page")
            if start_line >= total:
                return result(
                    f"[{path} — {total} lines total; sha256={sha}; continuation is past "
                    "the end of the file; re-read from offset 0]"
                )
            if start_char > len(lines[start_line]):
                return result(f"Error: {TOKEN_BEYOND_LINE}", "invalid_page")
        else:
            if not total:
                return result(f"[{path} — 0 lines total; empty file; sha256={sha}]",
                              complete=True, next_token=None)
            start_line, start_char = max(int(offset or 0), 0), 0
            if start_line >= total:
                return result(
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
            return result(f"[{path} — {total} lines total; sha256={sha}; no lines returned]")
        body = "\n".join(
            f"{number + 1}: {segment}" if whole and at == 0
            else f"{number + 1}:{at}: {segment}"
            for number, at, segment, whole in entries
        )
        end_of_file = index >= total
        token = None
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
        return result(
            "\n".join(part for part in (header, body, footer) if part),
            start_line=entries[0][0] + 1, end_line=entries[-1][0] + 1,
            start_character=entries[0][1], end_character=entries[-1][1] + len(entries[-1][2]),
            next_token=token, complete=end_of_file,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return result(f"Error: {exc}", "read_failed")


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
