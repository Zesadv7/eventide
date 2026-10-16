"""Tests for basic tools."""

import re

from eventide.tools.filesystem import run_edit, run_glob, run_read, run_write
from eventide.utils import decode_output


def test_decode_output_handles_utf8_and_console_encoding(monkeypatch):
    assert decode_output("中文".encode()) == "中文"
    monkeypatch.setattr("eventide.utils.console_encoding", lambda: "gbk")
    assert decode_output("错误".encode("gbk")) == "错误"
    assert isinstance(decode_output(b"\xff\xfe\xfa"), str)
    assert decode_output(None) == ""
    assert decode_output(b"") == ""


def test_run_write_and_read(isolated_workspace):
    assert run_write("test.txt", "hello", cwd=isolated_workspace) == "Wrote 5 bytes to test.txt"
    assert run_read("test.txt", cwd=isolated_workspace) == "1: hello"


def test_run_read_pages_large_files_and_reports_extent(isolated_workspace):
    lines = [f"line {index} " + "x" * 60 for index in range(200)]
    (isolated_workspace / "big.txt").write_text("\n".join(lines), encoding="utf-8")
    first = run_read("big.txt", cwd=isolated_workspace)
    assert first.startswith("[big.txt — 200 lines total; showing lines 1-")
    assert "continue with offset=" in first
    assert "more lines)" in first
    assert len(first) < 4000
    offset = int(re.search(r"offset=(\d+)", first).group(1))
    second = run_read("big.txt", offset=offset, cwd=isolated_workspace)
    assert second.startswith(f"[big.txt — 200 lines total; showing lines {offset + 1}-")
    assert "line 0 " not in second


def test_run_read_explicit_limit_reports_page(isolated_workspace):
    (isolated_workspace / "many.txt").write_text(
        "\n".join(f"l{index}" for index in range(10)), encoding="utf-8"
    )
    out = run_read("many.txt", limit=3, cwd=isolated_workspace)
    assert "showing lines 1-3" in out
    assert "l0" in out
    assert "l3" not in out
    assert "1: l0\n2: l1\n3: l2" in out


def test_run_read_preserves_source_coordinates_across_pages(isolated_workspace):
    (isolated_workspace / "source.py").write_bytes(
        'first\r\n\r\n变量 = "值"\r\nlast\r\n'.encode()
    )
    first = run_read("source.py", limit=2, cwd=isolated_workspace)
    assert "1: first\n2: " in first
    assert "continue with offset=2" in first
    second = run_read("source.py", offset=2, cwd=isolated_workspace)
    assert '3: 变量 = "值"\n4: last' in second
    assert "showing lines 3-4; end of file" in second
    assert "1: first" not in second


def test_run_edit(isolated_workspace):
    run_write("edit.txt", "old world", cwd=isolated_workspace)
    assert run_edit("edit.txt", "old", "new", cwd=isolated_workspace) == "Edited edit.txt"
    assert run_read("edit.txt", cwd=isolated_workspace) == "1: new world"


def test_run_glob(isolated_workspace):
    run_write("glob_a.txt", "a", cwd=isolated_workspace)
    run_write("glob_b.txt", "b", cwd=isolated_workspace)
    matches = run_glob("glob_*.txt", cwd=isolated_workspace).splitlines()
    assert "glob_a.txt" in matches
    assert "glob_b.txt" in matches
