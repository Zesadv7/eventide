"""MK-01/MK-02 tool-layer contracts: unique-match edits and version-bound paging.

The local tools, the production recovery path and the Docker file helper must
stay contract-identical, so each scenario is exercised on both backends where
the semantics can be observed without a real container.
"""

import hashlib
import re

import pytest

from eventide.execution_sandbox import SandboxFileTools
from eventide.models import CommandResult
from eventide.tool_contracts import ToolArgumentsError, ToolContract, builtin_schema
from eventide.tools.filesystem import run_edit, run_read
from tests.test_execution_sandbox_files import FakeContainer, sandbox

TOKEN_IN_OUTPUT = re.compile(r"evtfile1:\d+:\d+:[0-9a-f]{16}:[0-9a-f]{64}")


def continuation_token(output: str) -> str:
    match = re.search(r"continue with continuation (\S+)\]", output)
    assert match, output
    return match.group(1)


def test_read_reports_content_version_on_every_page(isolated_workspace):
    (isolated_workspace / "one.txt").write_bytes(b"hello\n")
    header, body = run_read("one.txt", cwd=isolated_workspace).splitlines()
    assert body == "1: hello"
    assert hashlib.sha256(b"hello\n").hexdigest() in header
    (isolated_workspace / "empty.txt").write_bytes(b"")
    assert "empty file; sha256=" in run_read("empty.txt", cwd=isolated_workspace)


def test_continuation_rejects_file_changed_between_pages(isolated_workspace):
    lines = [f"row {index} " + "p" * 90 for index in range(120)]
    (isolated_workspace / "rows.txt").write_text("\n".join(lines), encoding="utf-8")
    first = run_read("rows.txt", cwd=isolated_workspace)
    token = continuation_token(first)
    (isolated_workspace / "rows.txt").write_text("\n".join(lines[:60]), encoding="utf-8")
    second = run_read("rows.txt", continuation=token, cwd=isolated_workspace)
    assert second == "Error: File changed since the previous page; re-read it from offset 0."


def test_continuation_rejects_other_file_and_copy(isolated_workspace):
    content = "\n".join(["same bytes " + "s" * 100] * 200)
    for name in ("a.txt", "b.txt"):
        (isolated_workspace / name).write_text(content, encoding="utf-8")
    token = continuation_token(run_read("a.txt", cwd=isolated_workspace))
    assert "different file or working copy" in run_read(
        "b.txt", continuation=token, cwd=isolated_workspace
    )
    other = isolated_workspace / "copy"
    other.mkdir()
    (other / "a.txt").write_text(content, encoding="utf-8")
    assert "different file or working copy" in run_read("a.txt", continuation=token, cwd=other)


def test_continuation_rejects_malformed_forged_and_escaping_uses(isolated_workspace):
    (isolated_workspace / "real.txt").write_text(
        "\n".join("content " + "c" * 100 for _ in range(150)), encoding="utf-8"
    )
    token = continuation_token(run_read("real.txt", cwd=isolated_workspace))
    assert "Invalid continuation token" in run_read(
        "real.txt", continuation="evtfile1:1:0:zz:zz", cwd=isolated_workspace
    )
    forged = "evtfile1:5:5:" + token.split(":")[3] + ":" + "0" * 64
    assert "File changed since the previous page" in run_read(
        "real.txt", continuation=forged, cwd=isolated_workspace
    )
    # A token never bypasses path boundaries: the request path is still checked.
    assert "escapes workspace" in run_read(
        "../outside.txt", continuation=token, cwd=isolated_workspace
    )
    assert "not both" in run_read("real.txt", offset=1, continuation=token, cwd=isolated_workspace)


def test_legacy_line_offset_first_read_still_works(isolated_workspace):
    (isolated_workspace / "many.txt").write_text(
        "\n".join(f"n{index}" for index in range(10)), encoding="utf-8"
    )
    output = run_read("many.txt", offset=8, cwd=isolated_workspace)
    assert "showing lines 9-10; end of file" in output
    assert "9: n8\n10: n9" in output
    assert "past the end" in run_read("many.txt", offset=99, cwd=isolated_workspace)


def test_super_long_single_line_is_segmented_with_exact_coordinates(isolated_workspace):
    huge = "x" * 25_000
    (isolated_workspace / "long.txt").write_text(huge + "\ntail\n", encoding="utf-8")
    pieces: list[tuple[int, str]] = []
    output = run_read("long.txt", cwd=isolated_workspace)
    pages = 0
    while True:
        pages += 1
        for line in output.splitlines()[1:]:
            match = re.fullmatch(r"1:(\d+): (.*)", line)
            if match:
                pieces.append((int(match.group(1)), match.group(2)))
        if "continue with continuation" not in output:
            break
        output = run_read(
            "long.txt", continuation=continuation_token(output), cwd=isolated_workspace
        )
    assert pages == 3, output
    assert [offset for offset, _ in pieces] == [
        0,
        len(pieces[0][1]),
        len(pieces[0][1]) + len(pieces[1][1]),
    ]
    assert "".join(segment for _, segment in pieces) == huge
    assert f"line 1 continues at char {len(pieces[0][1])}" in run_read(
        "long.txt", cwd=isolated_workspace
    )
    assert output.splitlines()[-1] == "2: tail"
    assert "showing lines 1-2; end of file" in output


def test_crlf_and_unicode_offsets_are_code_point_accurate(isolated_workspace):
    line = "变量" * 5_000
    (isolated_workspace / "uni.txt").write_bytes(("first\r\n" + line + "\r\n结束").encode("utf-8"))
    first = run_read("uni.txt", cwd=isolated_workspace)
    body_lines = first.splitlines()[1:]
    assert body_lines[0] == "1: first"
    segment = body_lines[1]
    assert segment.startswith("2:0: ")
    shown = segment.removeprefix("2:0: ")
    assert shown == line[: len(shown)]
    second = run_read("uni.txt", continuation=continuation_token(first), cwd=isolated_workspace)
    rest = second.splitlines()[1]
    assert rest.startswith(f"2:{len(shown)}: ")
    assert shown + rest.removeprefix(f"2:{len(shown)}: ") == line
    assert second.splitlines()[2] == "3: 结束"


def test_edit_requires_exactly_one_match_and_changes_nothing(isolated_workspace):
    path = isolated_workspace / "code.txt"
    path.write_text("alpha\nkeep\nalpha\nalpha\n", encoding="utf-8")
    ambiguous = run_edit("code.txt", "alpha", "beta", cwd=isolated_workspace)
    assert ambiguous == (
        "Error: old_text matches 3 locations in code.txt (lines 1, 3, 4); the file is "
        "unchanged. Extend old_text with surrounding context so exactly one location matches."
    )
    missing = run_edit("code.txt", "nope", "beta", cwd=isolated_workspace)
    assert missing == (
        "Error: old_text not found in code.txt; the file is unchanged. Check exact "
        "whitespace, indentation and line endings; CRLF files include the carriage-return "
        "characters."
    )
    assert "old_text must not be empty" in run_edit("code.txt", "", "x", cwd=isolated_workspace)
    assert path.read_text() == "alpha\nkeep\nalpha\nalpha\n"
    many = "dup\n" * 8
    path.write_text(many, encoding="utf-8")
    bounded = run_edit("code.txt", "dup", "x", cwd=isolated_workspace)
    assert "matches at least 8 locations in code.txt (lines 1, 2, 3, 4, 5; more matches)" in bounded
    assert path.read_text() == many


def test_edit_expected_sha256_detects_changes_since_read(isolated_workspace):
    path = isolated_workspace / "watched.txt"
    path.write_text("hello\n", encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert (
        run_edit("watched.txt", "hello", "hi", expected_sha256=sha, cwd=isolated_workspace)
        == "Edited watched.txt"
    )
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("externally changed\n", encoding="utf-8")
    conflict = run_edit("watched.txt", "changed", "no", expected_sha256=sha, cwd=isolated_workspace)
    assert conflict.startswith("Error: Content version conflict in watched.txt:")
    assert "re-read the file; no change was applied." in conflict
    assert path.read_text() == "externally changed\n"
    assert "64-character lowercase sha256" in run_edit(
        "watched.txt", "changed", "no", expected_sha256="NOTHEX", cwd=isolated_workspace
    )


def test_edit_crlf_unique_match_keeps_other_lines(isolated_workspace):
    path = isolated_workspace / "crlf.txt"
    path.write_bytes(b"keep\r\ntarget\r\nkeep\r\n")
    assert run_edit("crlf.txt", "target\r\n", "done\r\n", cwd=isolated_workspace) == (
        "Edited crlf.txt"
    )
    assert path.read_bytes() == b"keep\r\ndone\r\nkeep\r\n"


def test_builtin_contracts_bound_continuation_and_expected_sha256():
    read_contract = ToolContract("read_file", builtin_schema("read_file"))
    token = "evtfile1:3:0:" + "a" * 16 + ":" + "b" * 64
    assert read_contract.validate({"path": "x", "continuation": token}) == {
        "path": "x",
        "continuation": token,
    }
    with pytest.raises(ToolArgumentsError):
        read_contract.validate({"path": "x", "continuation": "evtfile1:x:y:z:q"})
    edit_contract = ToolContract("edit_file", builtin_schema("edit_file"))
    validated = edit_contract.validate(
        {"path": "x", "old_text": "a", "new_text": "", "expected_sha256": "c" * 64}
    )
    assert validated["expected_sha256"] == "c" * 64
    with pytest.raises(ToolArgumentsError):
        edit_contract.validate(
            {"path": "x", "old_text": "a", "new_text": "", "expected_sha256": "nothex"}
        )


async def test_container_paging_matches_local_contract(tmp_path):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    lines = [f"row {index} " + "y" * 90 for index in range(150)]
    (root / "rows.txt").write_text("\n".join(lines), encoding="utf-8")
    files = SandboxFileTools(FakeContainer(), root)
    container_first = await files.read_file("rows.txt")
    local_first = run_read("rows.txt", cwd=root)
    assert TOKEN_IN_OUTPUT.sub("<tok>", container_first) == TOKEN_IN_OUTPUT.sub(
        "<tok>", local_first
    )
    container_rest = await files.read_file(
        "rows.txt", continuation=continuation_token(container_first)
    )
    local_rest = run_read("rows.txt", continuation=continuation_token(local_first), cwd=root)
    assert TOKEN_IN_OUTPUT.sub("<tok>", container_rest) == TOKEN_IN_OUTPUT.sub("<tok>", local_rest)
    # Pages are never stitched across a changed file.
    (root / "rows.txt").write_text("rewritten\n", encoding="utf-8")
    stale = await files.read_file("rows.txt", continuation=continuation_token(container_first))
    assert isinstance(stale, str)
    assert stale == "Error: File changed since the previous page; re-read it from offset 0."


async def test_container_long_line_parity_and_foreign_copy_rejection(tmp_path):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    huge = "z" * 25_000
    (root / "long.txt").write_text(huge + "\ntail\n", encoding="utf-8")
    files = SandboxFileTools(FakeContainer(), root)
    container_first = await files.read_file("long.txt")
    local_first = run_read("long.txt", cwd=root)
    assert TOKEN_IN_OUTPUT.sub("<tok>", container_first) == TOKEN_IN_OUTPUT.sub(
        "<tok>", local_first
    )
    second = await files.read_file("long.txt", continuation=continuation_token(container_first))
    offset = continuation_token(container_first).split(":")[2]
    assert f"1:{offset}: " in second
    # The same content restored into a second copy rejects the first copy's token.
    (tmp_path / "second").mkdir()
    _other_service, _other_binding, _other_source, other_root, _other_cwd = sandbox(
        tmp_path / "second"
    )
    (other_root / "long.txt").write_text(huge + "\ntail\n", encoding="utf-8")
    other_files = SandboxFileTools(FakeContainer(), other_root)
    rejected = await other_files.read_file(
        "long.txt", continuation=continuation_token(container_first)
    )
    assert isinstance(rejected, str)
    assert "different file or working copy" in rejected


async def test_container_edit_diagnostics_match_local_contract(tmp_path):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    (root / "code.txt").write_text("alpha\nkeep\nalpha\n", encoding="utf-8")
    files = SandboxFileTools(FakeContainer(), root)
    ambiguous = await files.edit_file("code.txt", "alpha", "beta")
    local = run_edit("code.txt", "alpha", "beta", cwd=root)
    assert isinstance(ambiguous, CommandResult)
    assert local in ambiguous.render()
    assert (root / "code.txt").read_text() == "alpha\nkeep\nalpha\n"
    sha = hashlib.sha256((root / "code.txt").read_bytes()).hexdigest()
    (root / "code.txt").write_text("moved on\n", encoding="utf-8")
    conflict = await files.edit_file("code.txt", "moved", "no", expected_sha256=sha)
    assert isinstance(conflict, CommandResult)
    assert run_edit("code.txt", "moved", "no", expected_sha256=sha, cwd=root) in conflict.render()
    assert (root / "code.txt").read_text() == "moved on\n"
    assert await files.edit_file("code.txt", "moved on", "applied") == "Edited code.txt"
    assert (root / "code.txt").read_text() == "applied\n"
