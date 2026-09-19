"""Sparse index build, incrementality, and UTF-8 filtering."""

import subprocess
from pathlib import Path

from eventide.rag.index import IndexStore


def _git_workspace(root: Path) -> Path:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".git" / "info" / "exclude").write_text(".eventide/\n", encoding="utf-8")
    return root


def test_sparse_index_build_search_incremental_and_force(isolated_workspace: Path) -> None:
    root = _git_workspace(isolated_workspace)
    source = root / "docs" / "auth.md"
    source.parent.mkdir()
    source.write_text("# 认证\n令牌验证由 verify_token 完成。\n", encoding="utf-8")
    store = IndexStore(root)

    first = store.build()
    assert first.files == 1 and first.rebuilt_files == 1 and first.chunks == 1
    hit = store.sparse_search("令牌验证", limit=5)[0]
    assert hit.chunk.file == "docs/auth.md"
    assert (hit.chunk.start_line, hit.chunk.end_line) == (1, 2)

    unchanged = store.build()
    assert unchanged.rebuilt_files == 0
    source.write_text("# 认证\n访问令牌由 check_token 验证。\n", encoding="utf-8")
    changed = store.build()
    assert changed.rebuilt_files == 1
    assert store.sparse_search("check_token", limit=5)[0].chunk.file == "docs/auth.md"

    forced = store.build(force=True)
    assert forced.rebuilt_files == 1


def test_build_removes_deleted_files_and_skips_binary(isolated_workspace: Path) -> None:
    root = _git_workspace(isolated_workspace)
    text = root / "keep.txt"
    text.write_text("searchable phrase", encoding="utf-8")
    binary = root / "binary.dat"
    binary.write_bytes(b"valid utf8\0but binary")
    store = IndexStore(root)

    first = store.build()
    assert first.files == 1 and first.skipped == 1
    text.unlink()
    second = store.build()
    assert second.files == 0 and second.deleted_files == 1 and second.chunks == 0


def test_non_git_workspace_is_rejected(isolated_workspace: Path) -> None:
    try:
        IndexStore(isolated_workspace)
    except ValueError as exc:
        assert str(exc) == "RAG indexing requires a Git workspace"
    else:  # pragma: no cover - explicit assertion message is clearer than pytest.raises here
        raise AssertionError("non-Git Workspace was accepted")

