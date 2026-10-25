"""Independent review: ambiguous overlapping edits and empty-file stale paging."""

import re

from eventide.tools.filesystem import run_edit, run_read


def test_overlapping_matches_are_ambiguous(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("aaa")
    result = run_edit("sample.txt", "aa", "b", cwd=tmp_path)
    assert result.startswith("Error:")
    assert path.read_text() == "aaa"


def test_continuation_rejects_a_file_emptied_since_first_page(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("x" * 20000)
    first = run_read("sample.txt", cwd=tmp_path)
    token = re.search(r"evtfile1:\d+:\d+:[0-9a-f]{16}:[0-9a-f]{64}", first).group()
    path.write_text("")
    result = run_read("sample.txt", continuation=token, cwd=tmp_path)
    assert result.startswith("Error:")
    assert "changed" in result.lower()


def test_page_budget_includes_line_numbers_and_header(tmp_path):
    from eventide.tools.filesystem import READ_PAGE_CHARS

    (tmp_path / "short.txt").write_text("x\n" * 10000)
    page = run_read("short.txt", cwd=tmp_path)
    assert len(page) <= READ_PAGE_CHARS
    assert "continue with continuation" in page
