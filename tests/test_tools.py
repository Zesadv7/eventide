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
    output = run_read("test.txt", cwd=isolated_workspace)
    header, body = output.splitlines()
    assert body == "1: hello"
    assert re.fullmatch(r"\[test\.txt — 1 lines total; sha256=[0-9a-f]{64}\]", header)


def test_run_read_pages_large_files_and_reports_extent(isolated_workspace):
    lines = [f"line {index} " + "x" * 60 for index in range(200)]
    (isolated_workspace / "big.txt").write_text("\n".join(lines), encoding="utf-8")
    first = run_read("big.txt", cwd=isolated_workspace)
    assert first.startswith("[big.txt — 200 lines total; sha256=")
    assert "showing lines 1-" in first
    assert re.search(r"\.\.\. \((\d+ more lines|line \d+ continues at char \d+)\)", first)
    assert len(first) < 10_200
    token = re.search(r"continue with continuation (\S+)\]", first).group(1)
    second = run_read("big.txt", continuation=token, cwd=isolated_workspace)
    assert second.startswith("[big.txt — 200 lines total; sha256=")
    assert "line 0 " not in second
    assert "end of file" in second


def test_run_read_explicit_limit_reports_page(isolated_workspace):
    (isolated_workspace / "many.txt").write_text(
        "\n".join(f"l{index}" for index in range(10)), encoding="utf-8"
    )
    out = run_read("many.txt", limit=3, cwd=isolated_workspace)
    assert "showing lines 1-3" in out
    assert "l0" in out
    assert "l3" not in out
    assert "1: l0\n2: l1\n3: l2" in out


def test_run_read_keeps_a_medium_source_file_in_one_page(isolated_workspace):
    lines = [f"value_{index} = " + '"' + "x" * 30 + '"' for index in range(183)]
    (isolated_workspace / "source.py").write_text("\n".join(lines))
    out = run_read("source.py", cwd=isolated_workspace)
    assert out.splitlines()[-1] == f"183: {lines[-1]}"
    assert "continue with" not in out
    assert len(out) <= 10_000


def test_run_read_preserves_source_coordinates_across_pages(isolated_workspace):
    (isolated_workspace / "source.py").write_bytes(
        'first\r\n\r\n变量 = "值"\r\nlast\r\n'.encode()
    )
    first = run_read("source.py", limit=2, cwd=isolated_workspace)
    assert "1: first\n2: " in first
    token = re.search(r"continue with continuation (\S+)\]", first).group(1)
    second = run_read("source.py", continuation=token, cwd=isolated_workspace)
    assert '3: 变量 = "值"\n4: last' in second
    assert "showing lines 3-4; end of file" in second
    assert "1: first" not in second


def test_run_edit(isolated_workspace):
    run_write("edit.txt", "old world", cwd=isolated_workspace)
    assert run_edit("edit.txt", "old", "new", cwd=isolated_workspace) == "Edited edit.txt"
    output = run_read("edit.txt", cwd=isolated_workspace)
    assert output.endswith("\n1: new world")


def test_run_glob(isolated_workspace):
    run_write("glob_a.txt", "a", cwd=isolated_workspace)
    run_write("glob_b.txt", "b", cwd=isolated_workspace)
    matches = run_glob("glob_*.txt", cwd=isolated_workspace).splitlines()
    assert "glob_a.txt" in matches
    assert "glob_b.txt" in matches
