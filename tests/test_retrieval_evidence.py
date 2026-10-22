"""Retrieval returns current, copy-bound evidence rather than stale indexed bodies."""

import hashlib
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from eventide.rag.embedding import HashEmbedder
from eventide.rag.index import MAX_SOURCE_BYTES, IndexStore
from eventide.rag.search import format_results, retrieval_from_vector, retrieve
from eventide.rag.tool import retrieval_output


def repository(path: Path, text: str = "stale_unique_marker") -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    (path / "source.txt").write_text(text, encoding="utf-8")
    return path.resolve()


def test_changed_file_does_not_return_old_indexed_body(tmp_path: Path) -> None:
    root = repository(tmp_path / "repo")
    index = IndexStore(root)
    index.build()
    (root / "source.txt").write_text("current implementation", encoding="utf-8")

    outcome = retrieve(root, "stale_unique_marker", embedder=HashEmbedder(),
                       profile=index.embedding_profile())
    rendered = format_results(list(outcome.results), mode=outcome.mode)

    assert "stale_unique_marker" not in rendered
    assert "source.txt" in rendered and "changed" in rendered


def test_external_copy_index_does_not_write_user_checkout(tmp_path: Path) -> None:
    root = repository(tmp_path / "repo")
    index_dir = tmp_path.resolve() / "state" / "indexes" / "copy-one"
    index = IndexStore(root, index_dir=index_dir)
    index.build()

    outcome = retrieve(root, "stale_unique_marker", embedder=HashEmbedder(),
                       profile=index.embedding_profile(), index_dir=index_dir)

    assert outcome.results[0].chunk.text == "stale_unique_marker"
    assert index.path == index_dir / "index.sqlite"
    assert not (root / ".eventide").exists()
    assert not (root / ".gitignore").exists()


def test_same_size_and_timestamp_still_requires_content_hash(tmp_path: Path) -> None:
    root = repository(tmp_path / "repo", "old needle")
    index = IndexStore(root)
    index.build()
    source = root / "source.txt"
    before = source.stat()
    source.write_text("new needle", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    outcome = retrieval_from_vector(root, "needle", vector=None, profile=None)

    assert outcome.results[0].freshness == "changed"
    assert outcome.results[0].chunk.text == ""
    assert outcome.results[0].chunk.content_hash == hashlib.sha256(b"old needle").hexdigest()


@pytest.mark.parametrize("change,expected", [
    ("delete", "deleted"), ("link", "unsafe"), ("directory", "unsafe"),
    ("large", "too_large"), ("denied", "unreadable"),
])
def test_invalid_source_never_returns_its_old_body(tmp_path: Path, monkeypatch, change, expected):
    root = repository(tmp_path / "repo")
    index = IndexStore(root)
    index.build()
    source = root / "source.txt"
    if change in {"delete", "link", "directory"}:
        source.unlink()
    if change == "link":
        outside = tmp_path / "outside"
        outside.write_text("external private data")
        source.symlink_to(outside)
    elif change == "directory":
        source.mkdir()
    elif change == "large":
        source.write_bytes(b"a" * (MAX_SOURCE_BYTES + 1))
    elif change == "denied":
        original_open = os.open

        def denied(path, *args, **kwargs):
            if path == source:
                raise PermissionError("injected source permission denial")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)

    outcome = retrieval_from_vector(root, "stale_unique_marker", vector=None, profile=None)
    output = retrieval_output(outcome, workspace_id="copy-one")

    assert outcome.results[0].freshness == expected
    assert outcome.results[0].chunk.text == outcome.results[0].chunk.heading == ""
    assert expected in output.content and "search_text" in output.content
    assert "stale_unique_marker" not in output.content
    assert "external private data" not in output.content
    assert output.metadata["retrieval"]["hits"][0]["freshness"] == expected


def test_source_ancestor_link_is_rejected_without_reading_external_body(tmp_path: Path):
    root = repository(tmp_path / "repo")
    source_dir = root / "src"
    source_dir.mkdir()
    (source_dir / "module.txt").write_text("needle old body")
    index = IndexStore(root)
    index.build()
    (source_dir / "module.txt").unlink()
    source_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "module.txt").write_text("external private body")
    source_dir.symlink_to(outside, target_is_directory=True)

    outcome = retrieval_from_vector(root, "needle", vector=None, profile=None)

    assert outcome.results[0].freshness == "unsafe"
    assert outcome.results[0].chunk.text == ""


def test_body_hash_profile_and_generation_belong_to_one_snapshot(tmp_path: Path, monkeypatch):
    root = repository(tmp_path / "repo", "original needle")
    index_dir = tmp_path / "state" / "copy-one"
    index = IndexStore(root, index_dir=index_dir)
    index.build()
    profile = index.embedding_profile()
    old_generation = index.generation()
    original_dense = IndexStore.dense_search

    def replace_after_dense(reader, vector, *, limit):
        hits = original_dense(reader, vector, limit=limit)
        (root / "source.txt").write_text("replacement unrelated")
        index.build(force=True)
        return hits

    monkeypatch.setattr(IndexStore, "dense_search", replace_after_dense)
    outcome = retrieve(root, "needle", embedder=HashEmbedder(), profile=profile,
                       index_dir=index_dir)
    output = retrieval_output(outcome, workspace_id="copy-one")

    assert outcome.index_generation == old_generation
    assert outcome.embedding_fingerprint == profile.fingerprint()
    assert index.generation() == old_generation + 1
    result = outcome.results[0]
    assert result.sources == ("dense", "sparse")
    assert result.chunk.content_hash == hashlib.sha256(b"original needle").hexdigest()
    assert result.chunk.text == "" and result.freshness == "changed"
    assert output.metadata["retrieval"]["index_generation"] == old_generation
    assert output.metadata["retrieval"]["workspace_id"] == "copy-one"
    assert "original needle" not in str(output)


def test_two_registered_copy_paths_have_independent_indexes(tmp_path: Path):
    first = repository(tmp_path / "first", "alpha copy implementation")
    second = repository(tmp_path / "second", "beta copy implementation")
    first_dir = tmp_path / "state" / "one"
    second_dir = tmp_path / "state" / "two"
    first_store = IndexStore(first, index_dir=first_dir)
    second_store = IndexStore(second, index_dir=second_dir)
    first_store.build()
    second_store.build()
    (first / "source.txt").write_text("gamma copy implementation")
    first_store.build()

    first_result = retrieval_from_vector(first, "implementation", vector=None, profile=None,
                                         index_dir=first_dir)
    second_result = retrieval_from_vector(second, "implementation", vector=None, profile=None,
                                          index_dir=second_dir)

    assert first_result.results[0].chunk.text == "gamma copy implementation"
    assert second_result.results[0].chunk.text == "beta copy implementation"
    assert first_result.index_generation == 2 and second_result.index_generation == 1
    assert first_result.results[0].freshness == second_result.results[0].freshness == "fresh"


def test_external_index_parent_link_is_rejected_before_creation(tmp_path: Path):
    root = repository(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "redirected").symlink_to(outside, target_is_directory=True)
    index = IndexStore(root, index_dir=tmp_path / "redirected" / "not-created")

    with pytest.raises(ValueError, match="index path"):
        index.build()
    assert list(outside.iterdir()) == []


def test_legacy_sparse_identity_does_not_invent_embedding_metadata(tmp_path: Path):
    root = repository(tmp_path / "repo")
    index = IndexStore(root)
    index.build()
    with sqlite3.connect(index.path) as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.execute("DELETE FROM meta WHERE key LIKE 'embedding_%'")

    outcome = retrieval_from_vector(root, "stale_unique_marker", vector=None, profile=None)

    assert outcome.embedding_fingerprint is None
    assert outcome.index_generation == 1
    assert outcome.results[0].freshness == "fresh"


def test_only_five_final_files_are_checked_and_empty_result_is_distinct(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo", "needle first")
    for number in range(10):
        (root / f"source-{number}.txt").write_text(f"needle {number}")
    index = IndexStore(root)
    index.build()
    checks = []
    freshness = IndexStore.file_freshness

    def record(self, relative, content_hash):
        checks.append(relative)
        return freshness(self, relative, content_hash)

    monkeypatch.setattr(IndexStore, "file_freshness", record)
    outcome = retrieval_from_vector(root, "needle", vector=None, profile=None, k=10)
    assert len(checks) == len(outcome.results) == 5
    missing = retrieval_from_vector(root, "absent-phrase", vector=None, profile=None)
    assert missing.results == () and missing.diagnostics == ()
    assert missing.index_generation == 1


async def test_rewrite_uses_external_snapshot_and_retains_typed_identity(tmp_path):
    from eventide.host import RuntimeHost
    from eventide.providers import ScriptedProvider
    from eventide.retrieval_rewrite import with_query_rewrite
    from tests.test_budget_api import new_run
    from tests.test_runtime import settings_for

    root = repository(tmp_path / "repo", "token verification credentials")
    index_dir = tmp_path / "state" / "indexes" / "copy-one"
    IndexStore(root, index_dir=index_dir).build()
    settings = replace(settings_for(root), state_dir=tmp_path / "runtime")
    host = RuntimeHost(settings, ScriptedProvider([{"text": "credentials"}]), enable_mcp=False)
    new_run(host, "root")

    async def search(query, k):
        return retrieval_from_vector(root, query, vector=None, profile=None, k=k,
                                     index_dir=index_dir)

    try:
        handler = with_query_rewrite(host, "root", root, search, index_dir=index_dir,
                                     workspace_id="copy-one")
        result = await handler("verification")
        evidence = result.metadata["retrieval"]
        assert evidence["index_generation"] == 1
        assert evidence["workspace_id"] == "copy-one"
        assert evidence["mode"] == "query-rrf"
        assert evidence["hits"][0]["freshness"] == "fresh"
        assert not (root / ".eventide").exists()
    finally:
        await host.close()
