"""Incremental reuse and failure atomicity at the Workspace filesystem boundary."""

import os
import shutil
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eventide.rag import index as index_module
from eventide.rag.embedding import HashEmbedder
from eventide.rag.index import IndexStore


@pytest.fixture
def index(tmp_path: Path) -> IndexStore:
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / ".git/info/exclude").write_text(".eventide/\n", encoding="utf-8")
    (root / "original.txt").write_text("original needle", encoding="utf-8")
    store = IndexStore(root)
    store.build()
    return store


def test_tracked_source_ancestor_link_never_reads_or_embeds_external_text(index, tmp_path):
    source = index.workspace / "docs/source.txt"
    source.parent.mkdir()
    source.write_text("local source", encoding="utf-8")
    subprocess.run(["git", "-C", str(index.workspace), "add", "docs/source.txt"], check=True)
    index.build()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.txt").write_text("external secret", encoding="utf-8")
    source.unlink()
    source.parent.rmdir()
    source.parent.symlink_to(outside, target_is_directory=True)

    class RejectUnexpectedEmbedding:
        def embed(self, texts):
            raise AssertionError("unchanged local files must reuse vectors")

    stats = index.build(embedder=RejectUnexpectedEmbedding(), profile=index.embedding_profile())

    assert stats.files == 1 and stats.deleted_files == 1 and stats.rebuilt_files == 0
    assert index.sparse_search("external", limit=5) == []
    assert index.sparse_search("local source", limit=5) == []


@pytest.mark.parametrize("ancestor", [False, True])
def test_source_links_inside_workspace_are_skipped(index, ancestor):
    source = index.workspace / "original.txt"
    if ancestor:
        directory = index.workspace / "real"
        directory.mkdir()
        source.rename(directory / "original.txt")
        (index.workspace / "alias").symlink_to(directory, target_is_directory=True)
        paths = ["real/original.txt", "alias/original.txt"]
    else:
        (index.workspace / "alias.txt").symlink_to(source)
        paths = ["original.txt", "alias.txt"]
    index._git_paths = lambda: paths
    scan = index._scan(include_chunks=True)
    assert len(scan.sources) == 1 and scan.skipped == 1
    assert not scan.sources[0].path.startswith("alias")


@pytest.mark.parametrize(
    "path", ["../outside.txt", "/outside.txt", "a/../outside.txt", "..\\outside.txt"],
)
def test_noncanonical_git_entries_cannot_escape_workspace(index, path):
    index._git_paths = lambda: [path]
    scan = index._scan(include_chunks=True)
    assert scan.sources == () and scan.skipped == 1


@pytest.mark.parametrize(
    "artifact", ["parent", "index.sqlite", "index.sqlite-wal", "index.sqlite-shm",
                 "index.sqlite-journal"],
)
def test_linked_index_artifacts_are_rejected_before_read_or_write(index, tmp_path, artifact):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside content remains untouched")
    if artifact == "parent":
        index.path.parent.rename(tmp_path / "saved-index")
        index.path.parent.symlink_to(outside, target_is_directory=True)
    else:
        target = index.path.parent / artifact
        target.unlink(missing_ok=True)
        target.symlink_to(sentinel)

    with pytest.raises(ValueError, match="index path"):
        index.build()
    with pytest.raises(ValueError, match="index path"):
        index.sparse_search("original", limit=5)
    with pytest.raises(ValueError, match="index path"), index.snapshot():
        pytest.fail("unsafe snapshot opened")
    assert sentinel.read_bytes() == b"outside content remains untouched"
    assert list(outside.iterdir()) == [sentinel]


def test_hardlinked_database_is_rejected_without_modifying_external_file(index, tmp_path):
    alias = tmp_path / "external.sqlite"
    os.link(index.path, alias)
    before = alias.read_bytes()
    with pytest.raises(ValueError, match="index path"):
        index.build()
    assert alias.read_bytes() == before


@pytest.mark.parametrize("link", [False, True])
def test_workspace_binding_does_not_follow_a_replaced_root(index, tmp_path, link):
    saved = tmp_path / "saved-repo"
    index.workspace.rename(saved)
    if link:
        index.workspace.symlink_to(saved, target_is_directory=True)
    else:
        index.workspace.mkdir()
    with pytest.raises(ValueError, match="Workspace path changed"):
        index.build()
    with pytest.raises(ValueError, match="Workspace path changed"), index.snapshot():
        pytest.fail("replaced Workspace opened")
    if not link:
        assert not (index.workspace / ".eventide").exists()


def test_index_path_cannot_be_rebound_outside_workspace(index, tmp_path):
    index.path = tmp_path / "external.sqlite"
    with pytest.raises(ValueError, match="remain inside its Workspace"):
        index.build()
    assert not index.path.exists()


def test_index_directory_alias_is_rejected_even_if_lstat_reports_directory(
    index, tmp_path, monkeypatch,
):
    resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        # Directory junctions on Windows need not have S_IFLNK in lstat.
        if path == index.path.parent:
            return tmp_path / "external"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="index path"):
        index.build()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("create", [False, True])
def test_connection_is_bound_to_the_file_checked_before_sqlite_open(
    index, tmp_path, monkeypatch, existing, create,
):
    if not existing:
        index.path.unlink()
    if not create and not existing:
        with pytest.raises(ValueError, match="No knowledge index"):
            index._connect(create=False)
        return
    connect = sqlite3.connect
    opened = []

    def replaced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        saved = tmp_path / "saved.sqlite"
        index.path.rename(saved)
        shutil.copy2(saved, index.path)
        return connection

    monkeypatch.setattr(sqlite3, "connect", replaced)
    with pytest.raises(RuntimeError, match="index path changed while opening"):
        index._connect(create=create)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_file_growth_cannot_bypass_the_source_read_limit(index, monkeypatch):
    source = index.workspace / "original.txt"
    source.write_bytes(b"x" * 1000)
    monkeypatch.setattr(index_module, "MAX_SOURCE_BYTES", 64)
    fstat = os.fstat
    fdopen = os.fdopen
    reads = []

    def small_stat(fd):
        actual = fstat(fd)
        return SimpleNamespace(
            st_mode=actual.st_mode, st_size=1, st_dev=actual.st_dev,
            st_ino=actual.st_ino, st_mtime_ns=actual.st_mtime_ns,
        )

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, limit):
            reads.append(limit)
            return self.stream.read(limit)

    monkeypatch.setattr(os, "fstat", small_stat)
    monkeypatch.setattr(os, "fdopen", lambda *args: Stream(fdopen(*args)))
    scan = index._scan(include_chunks=True)
    assert scan.sources == () and scan.skipped == 1 and reads == [65]


@pytest.mark.parametrize("link", [False, True])
def test_index_directory_replacement_during_embedding_cannot_publish(
    index, tmp_path, monkeypatch, link,
):
    (index.workspace / "original.txt").write_text("replacement needle", encoding="utf-8")
    embed = index._embed_chunks
    saved = tmp_path / "saved-index"

    def replace_index(sources, embedder):
        result = embed(sources, embedder)
        index.path.parent.rename(saved)
        if link:
            index.path.parent.symlink_to(saved, target_is_directory=True)
        else:
            index.path.parent.mkdir()
            shutil.copy2(saved / "index.sqlite", index.path)
        return result

    monkeypatch.setattr(index, "_embed_chunks", replace_index)
    with pytest.raises((ValueError, RuntimeError), match="index path"):
        index.build()
    # Inspect the original artifact directly: it must still contain only the old snapshot.
    with sqlite3.connect(saved / "index.sqlite") as connection:
        assert connection.execute("SELECT text FROM chunks").fetchall() == [("original needle",)]


def test_source_ancestor_link_inserted_during_embedding_rolls_back(index, tmp_path, monkeypatch):
    source = index.workspace / "docs/source.txt"
    source.parent.mkdir()
    source.write_text("local original", encoding="utf-8")
    subprocess.run(["git", "-C", str(index.workspace), "add", "docs/source.txt"], check=True)
    index.build()
    source.write_text("local changed", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.txt").write_text("external secret", encoding="utf-8")
    embed = index._embed_chunks

    def replace_source(sources, embedder):
        result = embed(sources, embedder)
        source.unlink()
        source.parent.rmdir()
        source.parent.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(index, "_embed_chunks", replace_source)
    with pytest.raises(RuntimeError, match="Workspace changed"):
        index.build()
    assert index.sparse_search("original", limit=5)
    assert index.sparse_search("changed", limit=5) == []
    assert index.sparse_search("external", limit=5) == []


def test_embedding_runs_outside_write_transaction_and_new_generation_wins(index, monkeypatch):
    (index.workspace / "original.txt").write_text("replacement needle", encoding="utf-8")
    embed = index._embed_chunks
    winner = None

    def concurrent_build(sources, embedder):
        nonlocal winner
        result = embed(sources, embedder)
        # A real second connection can commit while the first computes embeddings.
        IndexStore(index.workspace).build()
        with sqlite3.connect(index.path) as connection:
            winner = connection.execute(
                "SELECT value FROM meta WHERE key = 'generation'"
            ).fetchone()
        return result

    monkeypatch.setattr(index, "_embed_chunks", concurrent_build)
    with pytest.raises(RuntimeError, match="index changed during build"):
        index.build()
    with sqlite3.connect(index.path) as connection:
        current = connection.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
        assert current == winner
    assert index.sparse_search("replacement", limit=5)[0].chunk.text == "replacement needle"


def test_content_hash_not_timestamp_controls_vector_reuse_and_deletion(index):
    source = index.workspace / "original.txt"
    original = source.stat()
    source.write_text("modified needle", encoding="utf-8")
    assert source.stat().st_size == original.st_size
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    (index.workspace / "added.txt").write_text("added content", encoding="utf-8")
    batches = []

    class RecordingEmbedder:
        def embed(self, texts):
            batches.extend(texts)
            return HashEmbedder().embed(texts)

    profile = index.embedding_profile()
    stats = index.build(embedder=RecordingEmbedder(), profile=profile)
    assert stats.rebuilt_files == 2 and sorted(batches) == ["added content", "modified needle"]
    batches.clear()
    (index.workspace / "added.txt").unlink()
    stats = index.build(embedder=RecordingEmbedder(), profile=profile)
    assert stats.rebuilt_files == 0 and stats.deleted_files == 1 and batches == []
    assert index.sparse_search("original", limit=5) == []
    assert index.sparse_search("added", limit=5) == []


@pytest.mark.parametrize("field", ["provider", "model", "base_url", "algorithm_version"])
def test_each_embedding_identity_field_prevents_old_vector_reuse(index, field):
    profile = replace(index.embedding_profile(), **{field: "different"})
    with pytest.raises(ValueError, match="configuration changed"):
        index.build(embedder=HashEmbedder(), profile=profile)
    assert index.sparse_search("original", limit=5)


@pytest.mark.parametrize(
    "field", ["embedding_fingerprint", "chunking_profile", "chunking_fingerprint"],
)
def test_corrupt_fingerprint_is_not_silently_repaired_by_reusing_vectors(index, field):
    with sqlite3.connect(index.path) as connection:
        connection.execute("UPDATE meta SET value = 'corrupt' WHERE key = ?", (field,))
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        index.build()
    with sqlite3.connect(index.path) as connection:
        value = connection.execute("SELECT value FROM meta WHERE key = ?", (field,)).fetchone()
        assert value == ("corrupt",)
    assert index.sparse_search("original", limit=5)
    assert index.build(force=True).rebuilt_files == 1
    assert index.embedding_profile().model == "hash-v1"


def test_embedding_failure_keeps_old_file_fts_vectors_and_metadata(index):
    (index.workspace / "original.txt").write_text("replacement needle", encoding="utf-8")
    with sqlite3.connect(index.path) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("files", "chunks", "meta")
        }

    class FailedEmbedder:
        def embed(self, texts):
            raise RuntimeError("injected embedding failure")

    with pytest.raises(RuntimeError, match="injected embedding failure"):
        index.build(embedder=FailedEmbedder(), profile=index.embedding_profile())
    with sqlite3.connect(index.path) as connection:
        for table, expected in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == expected
    assert index.sparse_search("original", limit=5)[0].chunk.text == "original needle"
    assert index.sparse_search("replacement", limit=5) == []
