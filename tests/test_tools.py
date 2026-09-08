"""Tests for basic tools."""

import subprocess
import sys

from nexus_agent.tools.bash import run_bash
from nexus_agent.tools.filesystem import run_edit, run_glob, run_read, run_write
from nexus_agent.utils import decode_output


def test_run_bash():
    assert run_bash("echo ok") == "ok"


def test_decode_output_handles_utf8_and_console_encoding(monkeypatch):
    assert decode_output("中文".encode()) == "中文"
    monkeypatch.setattr("nexus_agent.utils.console_encoding", lambda: "gbk")
    assert decode_output("错误".encode("gbk")) == "错误"
    assert isinstance(decode_output(b"\xff\xfe\xfa"), str)
    assert decode_output(None) == ""
    assert decode_output(b"") == ""


def test_run_bash_survives_missing_streams(monkeypatch):
    class Result:
        stdout = None
        stderr = None

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    assert run_bash("anything") == "(no output)"


def test_run_bash_returns_utf8_child_output(isolated_workspace):
    script = isolated_workspace / "emit_utf8.py"
    script.write_text(
        "import sys; sys.stdout.buffer.write('中文输出'.encode('utf-8'))", encoding="utf-8"
    )
    assert run_bash(f'"{sys.executable}" "{script}"', cwd=isolated_workspace) == "中文输出"


def test_run_write_and_read(isolated_workspace):
    assert run_write("test.txt", "hello", cwd=isolated_workspace) == "Wrote 5 bytes to test.txt"
    assert run_read("test.txt", cwd=isolated_workspace) == "hello"


def test_run_edit(isolated_workspace):
    run_write("edit.txt", "old world", cwd=isolated_workspace)
    assert run_edit("edit.txt", "old", "new", cwd=isolated_workspace) == "Edited edit.txt"
    assert run_read("edit.txt", cwd=isolated_workspace) == "new world"


def test_run_glob(isolated_workspace):
    run_write("glob_a.txt", "a", cwd=isolated_workspace)
    run_write("glob_b.txt", "b", cwd=isolated_workspace)
    matches = run_glob("glob_*.txt", cwd=isolated_workspace).splitlines()
    assert "glob_a.txt" in matches
    assert "glob_b.txt" in matches
