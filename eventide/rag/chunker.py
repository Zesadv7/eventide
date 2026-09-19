"""Structure-aware text chunking for knowledge indexes."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MIN_CHUNK_CHARS = 600
MAX_CHUNK_CHARS = 800
OVERLAP_CHARS = 100

_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One citation-ready passage from a Workspace-relative text file."""

    file: str
    start_line: int
    end_line: int
    heading: str
    text: str


def _line_starts(text: str) -> list[int]:
    starts = [0]
    starts.extend(index + 1 for index, character in enumerate(text) if character == "\n")
    return starts


def _line_number(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def _preferred_end(text: str, start: int) -> int:
    limit = min(start + MAX_CHUNK_CHARS, len(text))
    if limit == len(text):
        return limit
    minimum = min(start + MIN_CHUNK_CHARS, limit)
    blank = text.rfind("\n\n", minimum, limit)
    if blank >= minimum:
        return blank + 2
    newline = text.rfind("\n", minimum, limit)
    if newline >= minimum:
        return newline + 1
    return limit


def _overlap_start(text: str, start: int, end: int) -> int:
    desired = max(start + 1, end - OVERLAP_CHARS)
    previous_newline = text.rfind("\n", start, desired)
    candidate = previous_newline + 1 if previous_newline >= start else desired
    if candidate <= start:
        candidate = desired
    return min(candidate, end)


def _window_chunks(
    file: str,
    text: str,
    starts: list[int],
    section_start: int,
    section_end: int,
    heading: str,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    cursor = section_start
    while cursor < section_end:
        local = text[cursor:section_end]
        end = cursor + _preferred_end(local, 0)
        if not text[cursor:end].strip():
            break
        chunks.append(
            Chunk(
                file=file,
                start_line=_line_number(starts, cursor),
                end_line=_line_number(starts, max(cursor, end - 1)),
                heading=heading,
                text=text[cursor:end],
            )
        )
        if end >= section_end:
            break
        next_cursor = cursor + _overlap_start(local, 0, end - cursor)
        if next_cursor <= cursor:
            next_cursor = end
        cursor = next_cursor
    return chunks


def _markdown_sections(text: str) -> list[tuple[int, int, str]]:
    sections: list[tuple[int, int, str]] = []
    headings = ["", "", ""]
    section_start = 0
    section_heading = ""
    offset = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        fence_match = _FENCE.match(bare)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        heading_match = None if fence is not None else _HEADING.match(bare)
        if heading_match:
            if offset > section_start and text[section_start:offset].strip():
                sections.append((section_start, offset, section_heading))
            level = len(heading_match.group(1))
            headings[level - 1] = heading_match.group(2).strip()
            for index in range(level, len(headings)):
                headings[index] = ""
            section_start = offset
            section_heading = " > ".join(item for item in headings if item)
        offset += len(line)
    if section_start < len(text) and text[section_start:].strip():
        sections.append((section_start, len(text), section_heading))
    return sections


def chunk_text(file: str, text: str) -> list[Chunk]:
    """Split text deterministically while retaining exact one-based line ranges."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []
    relative = PurePosixPath(file).as_posix()
    starts = _line_starts(normalized)
    sections = (
        _markdown_sections(normalized)
        if PurePosixPath(relative).suffix.lower() == ".md"
        else [(0, len(normalized), "")]
    )
    chunks: list[Chunk] = []
    for section_start, section_end, heading in sections:
        chunks.extend(
            _window_chunks(
                relative,
                normalized,
                starts,
                section_start,
                section_end,
                heading,
            )
        )
    return chunks
