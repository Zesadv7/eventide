"""Deterministic citation boundaries for RAG chunks."""

from eventide.rag.chunker import MAX_CHUNK_CHARS, chunk_text


def test_markdown_headings_and_line_numbers() -> None:
    text = "# Guide\nintro\n## Deploy\nsteps\n### Docker\nrun it\n"

    chunks = chunk_text("docs/guide.md", text)

    assert [(chunk.start_line, chunk.end_line, chunk.heading) for chunk in chunks] == [
        (1, 2, "Guide"),
        (3, 4, "Guide > Deploy"),
        (5, 6, "Guide > Deploy > Docker"),
    ]
    assert chunks[1].text == "## Deploy\nsteps\n"


def test_markdown_ignores_headings_inside_fences() -> None:
    text = "# Real\n```python\n# not a heading\n```\n## Child\nbody\n"

    chunks = chunk_text("README.md", text)

    assert [chunk.heading for chunk in chunks] == ["Real", "Real > Child"]
    assert "# not a heading" in chunks[0].text


def test_long_text_uses_bounded_overlapping_windows() -> None:
    text = "".join(f"line {number:03d} content\n" for number in range(100))

    chunks = chunk_text("notes.txt", text)

    assert len(chunks) >= 2
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert chunks[1].start_line <= chunks[0].end_line
    assert chunks[0].text.splitlines()[-1] in chunks[1].text


def test_empty_text_has_no_chunks_and_long_line_keeps_accurate_line() -> None:
    assert chunk_text("empty.txt", " \n\n") == []
    chunks = chunk_text("single.py", "x" * 900)
    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [(1, 1), (1, 1)]



def test_long_line_after_newline_does_not_expand_overlap() -> None:
    text = "header\n" + "".join(chr(0x4E00 + i) for i in range(1800))
    chunks = chunk_text("unicode.txt", text)
    cursor = 0
    previous_end = 0
    for chunk in chunks:
        start = text.index(chunk.text, cursor)
        assert 0 <= previous_end - start <= 100
        assert len(chunk.text) <= 800
        previous_end = start + len(chunk.text)
        cursor = start + 1
    assert previous_end == len(text)


def test_whitespace_window_does_not_hide_later_content() -> None:
    text = " " * 1700 + "尾部必须保留\n"
    chunks = chunk_text("whitespace.txt", text)
    assert any("尾部必须保留" in chunk.text for chunk in chunks)
    assert all(len(chunk.text) <= 800 for chunk in chunks)


def test_markdown_long_sections_do_not_overlap_across_headings() -> None:
    text = "# 第一章\n" + "甲" * 1700 + "\n## 第二章\nshort\n"
    chunks = chunk_text("guide.md", text)
    assert all("第二章" not in chunk.text for chunk in chunks if chunk.heading == "第一章")
    assert chunks[-1].heading == "第一章 > 第二章"
    assert chunks[-1].text == "## 第二章\nshort\n"
