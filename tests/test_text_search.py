"""Index-free search retains source coordinates and read-only execution boundaries."""

import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest

from eventide.executor import ToolContext, ToolExecutor
from eventide.host import RuntimeHost
from eventide.models import RunRequest, ToolCall
from eventide.policy import PolicyEngine
from eventide.providers import ScriptedProvider
from eventide.tools import filesystem, regex_backend
from eventide.tools.runtime_catalog import HANDLERS, TOOLS
from tests.test_runtime import settings_for

RG_AVAILABLE = shutil.which(regex_backend.RG_BINARY) is not None
requires_rg = pytest.mark.skipif(not RG_AVAILABLE, reason="ripgrep backend not installed")


def test_literal_search_coordinates_paging_and_filter(tmp_path):
    (tmp_path / "a.py").write_bytes('first\r\n\r\n变量 = "a.*b"\r\na.*b\r\n'.encode())
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/b.py").write_text("axb\na.*b\n")
    (tmp_path / "skip.txt").write_text("a.*b")
    first = filesystem.run_search_text("a.*b", "**/*.py", limit=1, cwd=tmp_path)
    assert 'a.py:3: 变量 = "a.*b"' in first
    assert "next offset=1" in first
    second = filesystem.run_search_text("a.*b", "**/*.py", offset=1, cwd=tmp_path)
    assert "a.py:4: a.*b" in second and "sub/b.py:2: a.*b" in second
    assert "end of matches" in second and "skip.txt" not in second
    assert '3: 变量 = "a.*b"' in filesystem.run_read("a.py", cwd=tmp_path)
    assert "(no matches" in filesystem.run_search_text("missing", cwd=tmp_path)


def test_search_skips_binary_large_hidden_and_external_links(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("needle private")
    (root / "link").symlink_to(tmp_path / "outside.txt")
    (root / "binary").write_bytes(b"needle\x00")
    (root / "invalid").write_bytes(b"needle\xff")
    (root / "large").write_bytes(b"needle" + b"x" * filesystem.SEARCH_FILE_BYTES)
    (root / ".hidden").write_text("needle hidden")
    out = filesystem.run_search_text("needle", cwd=root)
    assert "(no matches" in out and "skipped 3" in out
    assert "private" not in out and "hidden" not in out
    assert ".hidden:1: needle hidden" in filesystem.run_search_text(
        "needle", pattern=".*", cwd=root,
    )
    with pytest.raises(PermissionError):
        filesystem.run_search_text("needle", "../*", cwd=root)


def test_search_output_and_scan_limits_are_explicit(tmp_path, monkeypatch):
    (tmp_path / "a").write_text(("needle" + "x" * 20_000 + "\n") * 80)
    # This file exceeds the file limit; narrow snippets are tested on a smaller source.
    (tmp_path / "b").write_text(("needle" + "x" * 500 + "\n") * 80)
    out = filesystem.run_search_text("needle", cwd=tmp_path)
    assert len(out) < 10_000 and "next offset=" in out
    monkeypatch.setattr(filesystem, "SEARCH_SCAN_FILES", 1)
    limited = filesystem.run_search_text("absent", cwd=tmp_path)
    assert "scan limit reached" in limited and "remaining files unsearched" in limited


def test_directory_links_are_not_traversed_and_nested_globs_keep_zero_depth(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("needle")
    (tmp_path / "src/loop").symlink_to(tmp_path, target_is_directory=True)
    out = filesystem.run_search_text("needle", "src/**/*.py", cwd=tmp_path)
    assert out.count("src/a.py:1:") == 1 and "loop" not in out
    assert "end of matches" in out


def test_enumeration_stops_and_precise_path_can_bypass_large_directory(tmp_path, monkeypatch):
    for index in range(10):
        (tmp_path / f"file{index}.py").write_text("needle")
    monkeypatch.setattr(filesystem, "SEARCH_SCAN_FILES", 3)
    out = filesystem.run_search_text("needle", cwd=tmp_path)
    assert "enumeration incomplete=True" in out and "scan limit reached" in out
    assert out.count(":1: needle") <= 3
    exact = filesystem.run_search_text("needle", "file9.py", cwd=tmp_path)
    assert "file9.py:1: needle" in exact and "enumeration incomplete=False" in exact


@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"query": "x", "limit": 51}, {"query": "x", "offset": -1},
    {"query": "x", "cwd": "/tmp"}, {"query": "x", "limit": True},
])
async def test_search_invalid_arguments_never_execute(tmp_path, arguments):
    called = []
    executor = ToolExecutor({"search_text": lambda **kw: called.append(kw)})
    result = await executor.execute(
        ToolCall("bad", "search_text", arguments), ToolContext(tmp_path, "r", PolicyEngine()),
    )
    assert result.metadata["code"] == "invalid_arguments" and not called


def test_search_rechecks_scope_if_a_file_changes_to_an_external_link(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    file = root / "source.py"
    file.write_text("needle")
    outside = tmp_path / "private"
    outside.write_text("needle private")
    collect = filesystem._search_paths

    def changed(base, pattern):
        result = collect(base, pattern)
        file.unlink()
        file.symlink_to(outside)
        return result

    monkeypatch.setattr(filesystem, "_search_paths", changed)
    with pytest.raises(PermissionError):
        filesystem.run_search_text("needle", cwd=root)


async def test_plan_search_uses_executor_cwd_without_index(tmp_path):
    (tmp_path / "source.py").write_text("target_symbol = 1\n")
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "search", "name": "search_text",
                         "arguments": {"query": "target_symbol", "pattern": "*.py"}}]},
        {"text": "located"},
    ])
    host = RuntimeHost(settings_for(tmp_path), provider)
    try:
        result = await host.run(RunRequest("locate", mode="plan"))
        assert result.status == "completed"
        assert "search_knowledge" not in {t["name"] for t in provider.requests[0].tools}
        tool = host.store.get_run(result.run_id)["tools"]["search"]
        assert not tool["is_error"] and "source.py:1: target_symbol = 1" in tool["content"]
        denied = await ToolExecutor(HANDLERS, tools=TOOLS).execute(
            ToolCall("escape", "search_text", {"query": "x", "pattern": "../*"}),
            ToolContext(tmp_path, "r", PolicyEngine()),
        )
        assert denied.metadata["code"] == "permission_denied"
    finally:
        await host.close()


def test_literal_context_lines_merge_and_distinguish_hits(tmp_path):
    (tmp_path / "code.py").write_text("a1\na2\nhit1\na4\na5\na6\nhit2\na8\n")
    out = filesystem.run_search_text("hit", context_lines=2, cwd=tmp_path)
    assert 'hits use "path:line:"; context lines use "path:line-"' in out
    assert out.count("code.py:") == 8  # two 5-line blocks merged into 7 lines + sha header
    assert "code.py:3: hit1" in out and "code.py:7: hit2" in out
    assert "code.py:1- a1" in out and "code.py:8- a8" in out
    assert "code.py:3- " not in out and "code.py:7- " not in out
    sha = hashlib.sha256((tmp_path / "code.py").read_bytes()).hexdigest()
    assert f"code.py sha256={sha}" in out


def test_literal_case_insensitive_and_unicode_positions(tmp_path):
    (tmp_path / "u.txt").write_text("NAÏVE_TOKEN later\nplain\n")
    assert "u.txt:1:" not in filesystem.run_search_text("naïve_token", cwd=tmp_path)
    out = filesystem.run_search_text("naïve_token", case_sensitive=False, cwd=tmp_path)
    assert "u.txt:1: NAÏVE_TOKEN later" in out
    long_line = "x" * 500 + "NEEDLE" + "y" * 500
    (tmp_path / "long.txt").write_text(long_line + "\n")
    clipped = filesystem.run_search_text("needle", case_sensitive=False, cwd=tmp_path)
    # Line is 1006 chars; window starts at 440 and shows 240, eliding 326.
    assert "long.txt:1: …" in clipped and "… (+326 chars)" in clipped


def test_long_lines_are_clipped_with_exact_elision_marker(tmp_path):
    (tmp_path / "long.txt").write_text("y" * 300 + "needle" + "z" * 400 + "\n")
    out = filesystem.run_search_text("needle", cwd=tmp_path)
    entry = out.splitlines()[2]
    # 60 chars of lead-in plus a 240-char window; 706-char line leaves 226 elided.
    assert entry.startswith("long.txt:1: …") and "needle" in entry
    assert entry.endswith("… (+226 chars)")
    assert len(entry) < 400


def test_empty_search_reports_skips_instead_of_whole_repo_result(tmp_path):
    (tmp_path / "binary").write_bytes(b"\x00needle")
    out = filesystem.run_search_text("needle", cwd=tmp_path)
    assert "(no matches in the files searched" in out
    assert "skipped 1" in out and "not a whole-repo empty result" in out


def test_search_version_credentials_bind_to_edit_file(tmp_path):
    (tmp_path / "doc.txt").write_text("keep me\n")
    sha = hashlib.sha256((tmp_path / "doc.txt").read_bytes()).hexdigest()
    found = filesystem.run_search_text("keep", cwd=tmp_path)
    assert f"sha256={sha}" in found
    edited = filesystem.run_edit(
        "doc.txt", "keep", "kept", expected_sha256=sha, cwd=tmp_path,
    )
    assert edited == "Edited doc.txt"


@requires_rg
def test_regex_mode_matches_with_real_backend(tmp_path):
    (tmp_path / "app.py").write_text("import os\n\ndef handler():\n    return TOKEN_1\n")
    (tmp_path / "util.py").write_text("TOKEN_2 = 2\n")
    out = filesystem.run_search_text(r"TOKEN_\d+", mode="regex", cwd=tmp_path)
    assert "mode=regex backend=ripgrep case=sensitive" in out
    assert "app.py:4:     return TOKEN_1" in out
    assert "util.py:1: TOKEN_2 = 2" in out
    sha = hashlib.sha256((tmp_path / "app.py").read_bytes()).hexdigest()
    assert f"app.py sha256={sha}" in out


@requires_rg
def test_regex_mode_with_context_and_case_insensitive(tmp_path):
    (tmp_path / "cfg.ini").write_text("alpha\nTimeOut=30\nomega\n")
    out = filesystem.run_search_text(
        r"timeout\s*=\s*\d+", mode="regex", case_sensitive=False, context_lines=1,
        cwd=tmp_path,
    )
    assert "case=insensitive" in out
    assert "cfg.ini:2: TimeOut=30" in out
    assert "cfg.ini:1- alpha" in out and "cfg.ini:3- omega" in out


@requires_rg
def test_regex_backend_error_surfaces_parse_failure(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n")
    out = filesystem.run_search_text("(unclosed", mode="regex", cwd=tmp_path)
    assert out.startswith("Error: regex backend rejected the search:")
    assert "regex parse error" in out


@requires_rg
def test_regex_respects_hidden_rules_and_never_expands_scope(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".hidden").write_text("needle hidden\n")
    (tmp_path / "outside.txt").write_text("needle private\n")
    (root / "link").symlink_to(tmp_path / "outside.txt")
    assert ".hidden:1: needle hidden" in filesystem.run_search_text(
        "needle", pattern=".hidden", mode="regex", cwd=root,
    )
    out = filesystem.run_search_text("needle", mode="regex", cwd=root)
    assert "private" not in out and "link" not in out
    with pytest.raises(PermissionError):
        filesystem.run_search_text("needle", "../*", mode="regex", cwd=root)


@requires_rg
def test_regex_deadline_reports_partial_and_never_hangs(tmp_path):
    for index in range(20):
        (tmp_path / f"f{index}.txt").write_text("needle\n" * 50)
    original = filesystem.SEARCH_REGEX_DEADLINE_SECONDS
    filesystem.SEARCH_REGEX_DEADLINE_SECONDS = 0.05
    try:
        started = time.monotonic()
        out = filesystem.run_search_text("needle", mode="regex", cwd=tmp_path)
        elapsed = time.monotonic() - started
    finally:
        filesystem.SEARCH_REGEX_DEADLINE_SECONDS = original
    assert elapsed < 5
    assert "regex deadline exceeded" in out and "remaining files unsearched" in out


@requires_rg
def test_regex_survives_patterns_that_backtrack_catastrophically_in_python(tmp_path):
    (tmp_path / "trap.txt").write_text("a" * 50_000 + "\n")
    started = time.monotonic()
    out = filesystem.run_search_text("(a+)+b", mode="regex", cwd=tmp_path)
    assert time.monotonic() - started < 10
    assert "(no matches" in out and "end of matches" in out


def test_regex_output_cap_reports_possible_missing_matches(tmp_path, monkeypatch):
    (tmp_path / "many.txt").write_text("hit\n" * 400)
    monkeypatch.setattr(regex_backend, "MAX_OUTPUT_BYTES", 1024)
    out = filesystem.run_search_text("hit", mode="regex", cwd=tmp_path)
    assert "regex output cap reached" in out and "matches may be missed" in out


def test_regex_matches_verified_against_locally_read_bytes(tmp_path, monkeypatch):
    (tmp_path / "real.txt").write_text("real needle\n")
    (tmp_path / "other.txt").write_text("unrelated\n")
    fabricated = [
        regex_backend.ReportedMatch("other.txt", 1, "injected secret"),
        regex_backend.ReportedMatch("real.txt", 1, "real needle"),
    ]

    def fake_batch(*_args: object, **_kwargs: object) -> regex_backend.BatchResult:
        return regex_backend.BatchResult(fabricated, "completed")

    monkeypatch.setattr(regex_backend, "run_regex_batch", fake_batch)
    out = filesystem.run_search_text("needle", mode="regex", cwd=tmp_path)
    assert "injected secret" not in out
    assert "unverified 1 changed files excluded" in out
    assert "real.txt:1: real needle" in out


def test_regex_mode_reports_missing_backend_without_degrading(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("needle\n")
    monkeypatch.setattr(regex_backend, "find_backend", lambda: None)
    out = filesystem.run_search_text("needle", mode="regex", cwd=tmp_path)
    assert out.startswith("Error: regex mode is unavailable")
    assert "Literal mode still works" in out
    assert "a.txt:1: needle" in filesystem.run_search_text("needle", cwd=tmp_path)


def test_regex_deadline_kills_and_reaps_the_backend_process(monkeypatch):
    class _FakePipe:
        def __init__(self, fd: int) -> None:
            self._fd = fd

        def fileno(self) -> int:
            return self._fd

        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            os.close(self._fd)

    class _HungProcess:
        def __init__(self) -> None:
            self.read_fd, self.write_fd = os.pipe()  # write end stays open: no EOF
            self.err_fd, self.err_write_fd = os.pipe()
            os.close(self.err_write_fd)
            self.stdout = _FakePipe(self.read_fd)
            self.stderr = _FakePipe(self.err_fd)
            self.killed = False
            self.waited = False

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> int:
            self.waited = True
            return 0

    hung = _HungProcess()
    monkeypatch.setattr(regex_backend.subprocess, "Popen", lambda *a, **k: hung)
    try:
        result = regex_backend.run_regex_batch(
            "rg", "needle", ["a.txt"], Path("/tmp"), case_sensitive=True,
            deadline_seconds=0.1,
        )
    finally:
        os.close(hung.write_fd)
    assert result.outcome == "timeout" and not result.matches
    assert hung.killed and hung.waited


async def test_search_new_arguments_are_strictly_validated(tmp_path):
    executor = ToolExecutor({"search_text": lambda **kw: "unreachable"})
    for arguments in (
        {"query": "x", "mode": "bogus"}, {"query": "x", "context_lines": 11},
        {"query": "x", "context_lines": -1}, {"query": "x", "case_sensitive": "yes"},
        {"query": "x", "mode": True},
    ):
        result = await executor.execute(
            ToolCall("bad", "search_text", arguments),
            ToolContext(tmp_path, "r", PolicyEngine()),
        )
        assert result.metadata["code"] == "invalid_arguments", arguments


@requires_rg
async def test_executor_dispatches_regex_search_in_worker_thread(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n")
    executor = ToolExecutor(HANDLERS, tools=TOOLS)
    result = await executor.execute(
        ToolCall("s", "search_text", {"query": "needle", "mode": "regex"}),
        ToolContext(tmp_path, "r", PolicyEngine()),
    )
    assert not result.is_error and "backend=ripgrep" in result.content
    assert "a.txt:1: needle" in result.content
    literal = await executor.execute(
        ToolCall("l", "search_text", {"query": "needle"}),
        ToolContext(tmp_path, "r", PolicyEngine()),
    )
    assert not literal.is_error and "backend=python" in literal.content
