"""File read/write/edit/glob tools."""

import os
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

from eventide.policy import resolve_scoped_path

READ_PAGE_CHARS = 10_000
SEARCH_FILE_BYTES = 1_048_576
SEARCH_SCAN_BYTES = 10 * SEARCH_FILE_BYTES
SEARCH_SCAN_FILES = 2_000


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


def _page(lines: list[str], budget: int) -> list[str]:
    """Longest prefix of lines whose joined length stays within the budget."""
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if kept and used + cost > budget:
            break
        kept.append(line)
        used += cost
    return kept


def run_read(path: str, limit: int | None = None, offset: int = 0, cwd: Path | None = None) -> str:
    """Read a text file, paging long files and reporting the file's full extent.

    offset/limit are line-based. Without an explicit limit the page is cut so the
    numbered reply stays within the page budget. Line numbers are one-based
    source coordinates, including when offset is nonzero; they are not file text.
    The header reports the total line count and the next zero-based offset.
    """
    try:
        base = (cwd or Path.cwd()).resolve()
        file_path = resolve_scoped_path(base, path)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        total = len(lines)
        start = max(int(offset or 0), 0)
        if total and start >= total:
            return f"[{path} — {total} lines total; offset {start} is past the end]"
        remaining = [f"{number}: {line}" for number, line in enumerate(lines[start:], start + 1)]
        page = remaining[: int(limit)] if limit is not None else _page(remaining, READ_PAGE_CHARS)
        cut = len(remaining) - len(page)
        body = "\n".join(page)
        if not cut and start == 0:
            return body
        end = start + len(page)
        where = f"showing lines {start + 1}-{end}" if page else "no lines returned"
        hint = f"continue with offset={end}" if cut else "end of file"
        header = f"[{path} — {total} lines total; {where}; {hint}]"
        footer = f"... ({cut} more lines)" if cut else ""
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


def run_edit(path: str, old_text: str, new_text: str, cwd: Path | None = None) -> str:
    """Replace the first occurrence of old_text with new_text in a file."""
    try:
        base = (cwd or Path.cwd()).resolve()
        file_path = resolve_scoped_path(base, path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
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
