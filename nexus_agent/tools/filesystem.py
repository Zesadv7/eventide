"""File read/write/edit/glob tools."""

from pathlib import Path

from nexus_agent.config import WORKDIR
from nexus_agent.policy import resolve_scoped_path

READ_PAGE_CHARS = 3_600


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
    whole reply stays under the runtime's 4,000-character text cap, and the header
    (kept at the front, so it survives any later clipping) reports the total line
    count and the next offset. Small files are returned verbatim.
    """
    try:
        base = cwd or WORKDIR
        file_path = resolve_scoped_path(base, path)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        total = len(lines)
        start = max(int(offset or 0), 0)
        if total and start >= total:
            return f"[{path} — {total} lines total; offset {start} is past the end]"
        remaining = lines[start:]
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
        base = cwd or WORKDIR
        file_path = resolve_scoped_path(base, path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(path: str, old_text: str, new_text: str, cwd: Path | None = None) -> str:
    """Replace the first occurrence of old_text with new_text in a file."""
    try:
        base = cwd or WORKDIR
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
        base = cwd or WORKDIR
        results = []
        for match in g.glob(pattern, root_dir=base):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as exc:
        return f"Error: {exc}"
