"""Tests for basic tools."""

from nexus_agent.tools.bash import run_bash
from nexus_agent.tools.filesystem import run_read, run_write, run_edit, run_glob


def test_run_bash():
    assert run_bash("echo ok") == "ok"


def test_run_write_and_read():
    assert run_write("test.txt", "hello") == "Wrote 5 bytes to test.txt"
    assert run_read("test.txt") == "hello"


def test_run_edit():
    run_write("edit.txt", "old world")
    assert run_edit("edit.txt", "old", "new") == "Edited edit.txt"
    assert run_read("edit.txt") == "new world"


def test_run_glob():
    run_write("glob_a.txt", "a")
    run_write("glob_b.txt", "b")
    matches = run_glob("glob_*.txt").splitlines()
    assert "glob_a.txt" in matches
    assert "glob_b.txt" in matches
