"""Sparse index build, incrementality, and UTF-8 filtering."""

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

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
    invalid = root / "invalid.txt"
    invalid.write_bytes(b"\xff\xfe")
    large = root / "large.txt"
    large.write_bytes(b"x" * (1024 * 1024 + 1))
    store = IndexStore(root)

    first = store.build()
    assert first.files == 1 and first.skipped == 3
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


def test_p1_index_is_backfilled_atomically_on_next_build(isolated_workspace: Path) -> None:
    root = _git_workspace(isolated_workspace)
    source = root / "legacy.txt"
    source.write_text("legacy searchable text", encoding="utf-8")
    body = source.read_bytes()
    store = IndexStore(root)
    connection = store._connect(create=True)
    try:
        store._ensure_schema(connection)
        stat = source.stat()
        connection.execute(
            "INSERT INTO files(path, mtime_ns, size, content_hash) VALUES (?, ?, ?, ?)",
            ("legacy.txt", stat.st_mtime_ns, stat.st_size, hashlib.sha256(body).hexdigest()),
        )
        connection.execute(
            "INSERT INTO chunks(file, ordinal, start_line, end_line, heading, text, vec) "
            "VALUES ('legacy.txt', 0, 1, 1, '', ?, NULL)",
            (body.decode(),),
        )
    finally:
        connection.close()

    stats = store.build()

    assert stats.rebuilt_files == 0
    check = sqlite3.connect(store.path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 2
        assert check.execute("SELECT COUNT(*) FROM chunks WHERE vec IS NULL").fetchone()[0] == 0
    finally:
        check.close()


def test_index_errors_are_explicit_and_do_not_need_network(isolated_workspace: Path) -> None:
    root = _git_workspace(isolated_workspace)
    store = IndexStore(root)
    with pytest.raises(ValueError, match="No knowledge index"):
        store.sparse_search("missing", limit=5)

    (root / "doc.txt").write_text("indexed content", encoding="utf-8")
    store.build()
    with pytest.raises(ValueError, match="must not be empty"):
        store.sparse_search("  ", limit=5)

    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE meta SET value = 'bad' WHERE key = 'embedding_fingerprint'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match="fingerprint"):
        store.embedding_profile()


def test_dense_search_rejects_corrupt_vector_dimension(isolated_workspace: Path) -> None:
    root = _git_workspace(isolated_workspace)
    (root / "doc.txt").write_text("indexed content", encoding="utf-8")
    store = IndexStore(root)
    store.build()
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("UPDATE chunks SET vec = ?", (b"\0\0\0\0",))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match="dimension is inconsistent"):
        store.dense_search([1.0] * 256, limit=5)
