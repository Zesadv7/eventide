"""Index-free search retains source coordinates and read-only execution boundaries."""

import pytest

from eventide.executor import ToolContext, ToolExecutor
from eventide.host import RuntimeHost
from eventide.models import RunRequest, ToolCall
from eventide.policy import PolicyEngine
from eventide.providers import ScriptedProvider
from eventide.tools import filesystem
from eventide.tools.runtime_catalog import HANDLERS, TOOLS
from tests.test_runtime import settings_for


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
