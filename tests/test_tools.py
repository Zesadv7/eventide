"""Tests for basic tools."""

from nexus_agent.tools.bash import run_bash
from nexus_agent.tools.filesystem import run_edit, run_glob, run_read, run_write


def test_run_bash():
    assert run_bash("echo ok") == "ok"


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
