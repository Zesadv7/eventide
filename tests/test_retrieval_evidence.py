"""Retrieval returns current, copy-bound evidence rather than stale indexed bodies."""

import subprocess
from pathlib import Path

from eventide.rag.embedding import HashEmbedder
from eventide.rag.index import IndexStore
from eventide.rag.search import format_results, retrieve


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
