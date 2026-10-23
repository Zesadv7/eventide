"""Real optional parsers, exact source citations, and consistent index identities."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from eventide.rag import code_structure
from eventide.rag.chunker import (
    MAX_CHUNK_CHARS,
    chunk_document,
    chunk_text,
    chunking_identity,
)
from eventide.rag.evaluation import run_retrieval_evaluation
from eventide.rag.index import IndexStore


def _require_parser(grammar: str) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip(grammar)


def _workspace(root: Path) -> IndexStore:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".git" / "info" / "exclude").write_text(".eventide/\n", encoding="utf-8")
    return IndexStore(root)


def test_python_class_nested_symbols_and_unicode_citations() -> None:
    _require_parser("tree_sitter_python")
    text = (
        'message = "中文😎"\n\n'
        "@decorate\nclass Outer:\n"
        '    """class docs"""\n'
        "    def first(self):\n"
        "        def 内部():\n"
        '            return "first 中文"\n'
        "        return 内部()\n\n"
        "    async def second(self):\n"
        '        return "second"\n\n'
        "def standalone():\n"
        "    return 3\n"
    )

    document = chunk_document("src/logic.py", text, code_structure=True)

    assert document.structured and document.fallback_reason is None
    assert document == chunk_document("src/logic.py", text, code_structure=True)
    inner = next(chunk for chunk in document.chunks if chunk.heading.endswith(" > 内部"))
    assert inner.heading == "Outer > first > 内部"
    assert (inner.start_line, inner.end_line) == (7, 8)
    assert inner.text == '        def 内部():\n            return "first 中文"'
    second = next(chunk for chunk in document.chunks if chunk.heading == "Outer > second")
    assert (second.start_line, second.end_line) == (11, 12)
    assert second.text in text
    assert any(chunk.text.startswith("@decorate\nclass Outer:") for chunk in document.chunks)
    combined = "".join(chunk.text for chunk in document.chunks)
    assert combined.count('return "first 中文"') == 1
    assert combined.count('return "second"') == 1
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS for chunk in document.chunks)


@pytest.mark.parametrize("suffix", ["js", "jsx", "mjs", "cjs"])
def test_javascript_functions_classes_and_arrow_bindings(suffix: str) -> None:
    _require_parser("tree_sitter_javascript")
    text = (
        "export class Cache {\n"
        "  constructor() { this.value = 0; }\n"
        "  get() { return this.value; }\n"
        "}\n"
        "export const refresh = async () => { return '刷新'; };\n"
        "export function reset() { return 0; }\n"
    )

    chunks = chunk_text(f"cache.{suffix}", text, code_structure=True)

    headings = {chunk.heading for chunk in chunks}
    assert {"Cache", "Cache > constructor", "Cache > get", "refresh", "reset"} <= headings
    refresh = next(chunk for chunk in chunks if chunk.heading == "refresh")
    assert refresh.text.startswith("export const refresh")
    assert (refresh.start_line, refresh.end_line) == (5, 5)
    assert sum(chunk.text.count("return this.value") for chunk in chunks) == 1
    assert all(chunk.text.strip() not in {"}", ";", "; }"} for chunk in chunks)


@pytest.mark.parametrize("suffix", ["ts", "tsx", "mts", "cts"])
def test_typescript_interface_methods_and_class_field_functions(suffix: str) -> None:
    _require_parser("tree_sitter_typescript")
    text = (
        "interface Reader {\n  read(): string;\n}\n"
        "export class Client implements Reader {\n"
        "  transform = (x: number): number => x + 1;\n"
        "  public read(): string { return 'read'; }\n}\n"
        "export function make(): Client { return new Client(); }\n"
    )
    if suffix == "tsx":
        text += "const View = () => <span>ok</span>;\n"

    document = chunk_document(f"client.{suffix}", text, code_structure=True)

    assert document.structured
    headings = {chunk.heading for chunk in document.chunks}
    assert {"Reader > read", "Client > transform", "Client > read", "make"} <= headings
    method = next(chunk for chunk in document.chunks if chunk.heading == "Client > read")
    assert (method.start_line, method.end_line) == (6, 6)
    if suffix == "tsx":
        assert "View" in headings


def test_oversized_source_symbol_remains_bounded_and_keeps_its_path() -> None:
    _require_parser("tree_sitter_python")
    text = "class Big:\n    def calculate(self):\n" + "        value += 1\n" * 200

    chunks = chunk_text("big.py", text, code_structure=True)

    windows = [chunk for chunk in chunks if chunk.heading == "Big > calculate"]
    assert len(windows) > 1
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert windows[0].start_line == 2 and windows[-1].end_line == 202
    assert windows[1].start_line <= windows[0].end_line


def test_abstract_typescript_class_and_decorated_python_method_paths() -> None:
    _require_parser("tree_sitter_typescript")
    _require_parser("tree_sitter_python")
    typescript = chunk_document(
        "base.ts", "abstract class Base { abstract read(): string; }", code_structure=True
    )
    assert typescript.structured
    assert any(chunk.heading == "Base > read" for chunk in typescript.chunks)

    python = chunk_document(
        "base.py",
        "class Base:\r\n    @cached\r\n    def read(self): return 1\r\n",
        code_structure=True,
    )
    method = next(chunk for chunk in python.chunks if chunk.heading == "Base > read")
    assert (method.start_line, method.end_line) == (2, 3)
    assert method.text == "    @cached\n    def read(self): return 1"


@pytest.mark.parametrize(
    ("file", "grammar", "text"),
    [
        ("broken.js", "tree_sitter_javascript", "function broken( {"),
        ("broken.ts", "tree_sitter_typescript", "class Broken { method( {"),
    ],
)
def test_real_broken_javascript_and_typescript_fall_back(
    file: str,
    grammar: str,
    text: str,
) -> None:
    _require_parser(grammar)
    document = chunk_document(file, text, code_structure=True)
    assert document.fallback_reason == "parse_error"
    assert list(document.chunks) == chunk_text(file, text)


def test_default_and_markdown_never_load_optional_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> None:
        raise AssertionError("optional parser was loaded")

    monkeypatch.setattr(code_structure, "_parser", forbidden)
    assert chunk_text("small.py", "def small():\n    pass\n")[0].heading == ""
    assert chunk_text("doc.md", "# Topic\nbody\n", code_structure=True)[0].heading == "Topic"
    assert json.loads(chunking_identity())["parsers"] is None


def test_missing_dependency_explicitly_falls_back_to_existing_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(suffix: str) -> None:
        raise ModuleNotFoundError("optional grammar absent")

    monkeypatch.setattr(code_structure, "_parser", unavailable)
    text = "def worker():\n" + "    value += 1\n" * 100

    document = chunk_document("worker.py", text, code_structure=True)

    assert document.fallback_reason == "dependency_unavailable"
    assert not document.structured
    assert list(document.chunks) == chunk_text("worker.py", text)


@pytest.mark.parametrize("error", [ValueError("ABI mismatch"), OSError("cannot load library")])
def test_incompatible_optional_parser_is_reported(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def incompatible(suffix: str) -> None:
        raise error

    monkeypatch.setattr(code_structure, "_parser", incompatible)
    document = chunk_document("worker.py", "def f(): pass", code_structure=True)
    assert document.fallback_reason == "parser_unavailable"
    assert list(document.chunks) == chunk_text("worker.py", "def f(): pass")


def test_unsupported_source_language_and_real_syntax_error_fall_back() -> None:
    text = "func run() { return 1 }\n"
    document = chunk_document("main.go", text, code_structure=True)
    assert document.fallback_reason == "unsupported_language"
    assert list(document.chunks) == chunk_text("main.go", text)

    _require_parser("tree_sitter_python")
    broken = "def broken(:\n    return 1\n"
    document = chunk_document("broken.py", broken, code_structure=True)
    assert document.fallback_reason == "parse_error"
    assert list(document.chunks) == chunk_text("broken.py", broken)


def test_import_and_default_chunking_ignore_blocked_parser_packages() -> None:
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('tree_sitter'):
        raise AssertionError('native optional dependency loaded')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from eventide.rag.index import IndexStore
from eventide.rag.chunker import chunk_text
assert chunk_text('a.py', 'def f(): pass')[0].heading == ''
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)


def test_index_configuration_change_requires_full_rebuild_and_preserves_citations(
    isolated_workspace: Path,
) -> None:
    _require_parser("tree_sitter_python")
    store = _workspace(isolated_workspace)
    (isolated_workspace / "worker.py").write_text(
        "class Worker:\n    def first(self):\n        return 'needle123'\n", encoding="utf-8"
    )
    store.build()
    with pytest.raises(ValueError, match="Chunking configuration changed"):
        store.build(code_structure=True)

    stats = store.build(force=True, code_structure=True)

    assert stats.structured_files == 1 and stats.structure_fallbacks == {}
    hit = store.sparse_search("needle123", limit=5)[0].chunk
    assert hit.heading == "Worker > first"
    assert (hit.start_line, hit.end_line) == (2, 3)
    assert store.build(code_structure=True).rebuilt_files == 0
    with pytest.raises(ValueError, match="Chunking configuration changed"):
        store.build()
    with sqlite3.connect(store.path) as connection:
        profile = connection.execute(
            "SELECT value FROM meta WHERE key = 'chunking_profile'"
        ).fetchone()[0]
    assert json.loads(profile)["code_structure"] is True
    assert json.loads(profile)["parsers"]["tree-sitter-python"] is not None


def test_index_parser_version_change_does_not_reuse_old_chunks(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _workspace(isolated_workspace)
    (isolated_workspace / "worker.py").write_text("def f(): pass\n", encoding="utf-8")
    store.build(code_structure=True)
    monkeypatch.setattr(code_structure.metadata, "version", lambda name: "future-version")

    with pytest.raises(ValueError, match="Chunking configuration changed"):
        store.build(code_structure=True)


def test_index_build_reports_missing_parser_and_unsupported_language(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _workspace(isolated_workspace)
    (isolated_workspace / "worker.py").write_text("def f(): pass\n", encoding="utf-8")
    (isolated_workspace / "main.go").write_text("package main\n", encoding="utf-8")

    def unavailable(suffix: str) -> None:
        raise ModuleNotFoundError("missing")

    monkeypatch.setattr(code_structure, "_parser", unavailable)
    stats = store.build(code_structure=True)

    assert stats.structured_files == 0
    assert stats.structure_fallbacks == {"dependency_unavailable": 1, "unsupported_language": 1}
    assert store.sparse_search("def", limit=5)[0].chunk.heading == ""


def test_old_window_index_requires_force_without_chunking_metadata(
    isolated_workspace: Path,
) -> None:
    store = _workspace(isolated_workspace)
    (isolated_workspace / "worker.py").write_text("def f(): pass\n", encoding="utf-8")
    store.build()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM meta WHERE key LIKE 'chunking_%'")

    assert store.sparse_search("def", limit=5)
    with pytest.raises(ValueError, match="Chunking configuration changed"):
        store.build()
    with pytest.raises(ValueError, match="Chunking configuration changed"):
        store.build(code_structure=True)
    assert store.build(force=True).rebuilt_files == 1


def test_parser_configuration_drift_rolls_back_index_rebuild(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _workspace(isolated_workspace)
    source = isolated_workspace / "worker.py"
    source.write_text("def f(): return 'originalneedle'\n", encoding="utf-8")
    store.build(code_structure=True)
    source.write_text("def f(): return 'replacementneedle'\n", encoding="utf-8")
    embed_chunks = store._embed_chunks

    def changed(sources, embedder):
        result = embed_chunks(sources, embedder)
        monkeypatch.setattr(code_structure.metadata, "version", lambda name: "changed-version")
        return result

    monkeypatch.setattr(store, "_embed_chunks", changed)
    with pytest.raises(RuntimeError, match="Chunking configuration changed during build"):
        store.build(code_structure=True)
    assert "originalneedle" in store.sparse_search("originalneedle", limit=5)[0].chunk.text
    assert store.sparse_search("replacementneedle", limit=5) == []


def test_retrieval_eval_reports_actual_structure_and_same_corpus_identity(
    isolated_workspace: Path,
) -> None:
    _require_parser("tree_sitter_python")
    corpus = isolated_workspace / "corpus"
    corpus.mkdir()
    (corpus / "worker.py").write_text(
        "class Worker:\n    def process(self):\n        return 'distinctive123'\n",
        encoding="utf-8",
    )
    suite = isolated_workspace / "suite.yaml"
    suite.write_text(
        "index: corpus\ncases:\n  - id: process\n    query: distinctive123\n"
        "    expect_files: [worker.py]\n    expect_contains: distinctive123\n",
        encoding="utf-8",
    )

    window = run_retrieval_evaluation(suite)
    structure = run_retrieval_evaluation(suite, code_structure=True)

    assert window["corpus_sha256"] == structure["corpus_sha256"]
    assert window["chunking_fingerprint"] != structure["chunking_fingerprint"]
    assert structure["structured_files"] == 1 and structure["structure_fallbacks"] == {}
    hit = structure["results"][0]["hits"][0]
    assert hit["heading"] == "Worker > process"
    assert (hit["start_line"], hit["end_line"]) == (2, 3)
    assert window["passed"] == structure["passed"] == 1


def test_long_unicode_function_keeps_symbol_and_bounded_overlap() -> None:
    _require_parser("tree_sitter_python")
    payload = "".join(chr(0x4E00 + i) for i in range(1800))
    text = f'def 读取():\n    return "{payload}"\n'
    document = chunk_document("unicode.py", text, code_structure=True)
    assert document.structured and document.fallback_reason is None
    cursor = 0
    previous_end = 0
    for chunk in document.chunks:
        start = text.index(chunk.text, cursor)
        assert 0 <= previous_end - start <= 100
        assert chunk.heading == "读取"
        assert len(chunk.text) <= MAX_CHUNK_CHARS
        assert (chunk.start_line, chunk.end_line) == (1 if start == 0 else 2, 2)
        previous_end = start + len(chunk.text)
        cursor = start + 1
    assert text[previous_end:].strip() == ""
