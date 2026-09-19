"""SQLite-backed Workspace knowledge index."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from eventide.rag.chunker import Chunk, chunk_text
from eventide.workspace import canonical_workspace, git

MAX_SOURCE_BYTES = 1024 * 1024
INDEX_RELATIVE_PATH = Path(".eventide") / "index.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    heading TEXT NOT NULL,
    text TEXT NOT NULL,
    vec BLOB,
    UNIQUE(file, ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO fts(fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text ON chunks BEGIN
    INSERT INTO fts(fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO fts(rowid, text) VALUES (new.id, new.text);
END;
"""


@dataclass(frozen=True, slots=True)
class BuildStats:
    files: int
    rebuilt_files: int
    deleted_files: int
    chunks: int
    skipped: int
    duration_ms: float


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    id: int
    file: str
    start_line: int
    end_line: int
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class SparseCandidate:
    chunk: IndexedChunk
    rank: int
    bm25: float


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: str
    mtime_ns: int
    size: int
    content_hash: str
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True, slots=True)
class _Scan:
    sources: tuple[_SourceFile, ...]
    skipped: int
    signature: tuple[tuple[str, str], ...]


class IndexStore:
    """Own the rebuildable knowledge index for one canonical Git Workspace."""

    def __init__(self, workspace: Path):
        root, git_root = canonical_workspace(workspace)
        if git_root is None:
            raise ValueError("RAG indexing requires a Git workspace")
        self.workspace = root
        self.path = root / INDEX_RELATIVE_PATH

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise ValueError("No knowledge index; run 'eventide rag build'")
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if create:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1}:
            raise ValueError(f"Unsupported knowledge index version: {version}")
        if version == 1:
            return
        try:
            connection.executescript(_SCHEMA)
        except sqlite3.OperationalError as exc:
            if "tokenizer" in str(exc).lower() or "fts5" in str(exc).lower():
                raise RuntimeError(
                    "SQLite FTS5 trigram tokenizer is unavailable in this build"
                ) from exc
            raise
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')"
        )

    def _git_paths(self) -> list[str]:
        raw = git(
            self.workspace,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        )
        return sorted(os.fsdecode(item) for item in raw.split(b"\0") if item)

    def _scan(self, *, include_chunks: bool) -> _Scan:
        sources: list[_SourceFile] = []
        signature: list[tuple[str, str]] = []
        skipped = 0
        for raw_path in self._git_paths():
            relative = PurePosixPath(raw_path).as_posix()
            parts = PurePosixPath(relative).parts
            if not parts or parts[0] in {".git", ".eventide"}:
                continue
            candidate = self.workspace / Path(*parts)
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    skipped += 1
                    signature.append((relative, "skipped"))
                    continue
                stat = candidate.stat()
                if stat.st_size > MAX_SOURCE_BYTES:
                    skipped += 1
                    signature.append((relative, f"large:{stat.st_mtime_ns}:{stat.st_size}"))
                    continue
                body = candidate.read_bytes()
            except OSError:
                skipped += 1
                signature.append((relative, "unreadable"))
                continue
            content_hash = hashlib.sha256(body).hexdigest()
            if b"\0" in body:
                skipped += 1
                signature.append((relative, f"binary:{content_hash}"))
                continue
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                signature.append((relative, f"non-utf8:{content_hash}"))
                continue
            signature.append((relative, content_hash))
            chunks = tuple(chunk_text(relative, text)) if include_chunks else ()
            sources.append(
                _SourceFile(
                    path=relative,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                    content_hash=content_hash,
                    chunks=chunks,
                )
            )
        return _Scan(tuple(sources), skipped, tuple(signature))

    @staticmethod
    def _generation(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'generation'"
        ).fetchone()
        return int(row[0]) if row else 0

    def build(self, *, force: bool = False) -> BuildStats:
        """Incrementally rebuild changed files and commit the new snapshot atomically."""
        started = time.perf_counter()
        connection = self._connect(create=True)
        try:
            self._ensure_schema(connection)
            generation = self._generation(connection)
            existing = {
                str(row["path"]): str(row["content_hash"])
                for row in connection.execute("SELECT path, content_hash FROM files")
            }
            scan = self._scan(include_chunks=True)
            current = {source.path: source for source in scan.sources}
            changed = [
                source
                for source in scan.sources
                if force or existing.get(source.path) != source.content_hash
            ]
            deleted = set(existing) - set(current)
            verified = self._scan(include_chunks=False)
            if verified.signature != scan.signature:
                raise RuntimeError("Workspace changed during index build; retry")

            connection.execute("BEGIN IMMEDIATE")
            try:
                if self._generation(connection) != generation:
                    raise RuntimeError("Knowledge index changed during build; retry")
                if force:
                    connection.execute("DELETE FROM files")
                else:
                    connection.executemany(
                        "DELETE FROM files WHERE path = ?",
                        [(path,) for path in sorted(deleted)],
                    )
                    connection.executemany(
                        "DELETE FROM files WHERE path = ?",
                        [(source.path,) for source in changed],
                    )
                for source in changed:
                    connection.execute(
                        "INSERT INTO files(path, mtime_ns, size, content_hash) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            source.path,
                            source.mtime_ns,
                            source.size,
                            source.content_hash,
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO chunks"
                        "(file, ordinal, start_line, end_line, heading, text, vec) "
                        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        [
                            (
                                source.path,
                                ordinal,
                                chunk.start_line,
                                chunk.end_line,
                                chunk.heading,
                                chunk.text,
                            )
                            for ordinal, chunk in enumerate(source.chunks)
                        ],
                    )
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('generation', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(generation + 1),),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            chunk_count = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        finally:
            connection.close()
        return BuildStats(
            files=len(scan.sources),
            rebuilt_files=len(changed),
            deleted_files=len(existing) if force else len(deleted),
            chunks=chunk_count,
            skipped=scan.skipped,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def sparse_search(self, query: str, *, limit: int) -> list[SparseCandidate]:
        """Return deterministic BM25 candidates from the trigram FTS index."""
        cleaned = query.strip()[:256]
        if not cleaned:
            raise ValueError("Search query must not be empty")
        terms = [term for term in cleaned.split() if term]
        match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        connection = self._connect(create=False)
        try:
            self._ensure_schema(connection)
            rows = connection.execute(
                "SELECT chunks.id, chunks.file, chunks.start_line, chunks.end_line, "
                "chunks.heading, chunks.text, bm25(fts) AS score "
                "FROM fts JOIN chunks ON chunks.id = fts.rowid "
                "WHERE fts MATCH ? ORDER BY score ASC, chunks.id ASC LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Invalid knowledge search query: {query}") from exc
        finally:
            connection.close()
        return [
            SparseCandidate(
                chunk=IndexedChunk(
                    id=int(row["id"]),
                    file=str(row["file"]),
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    heading=str(row["heading"]),
                    text=str(row["text"]),
                ),
                rank=rank,
                bm25=float(row["score"]),
            )
            for rank, row in enumerate(rows, 1)
        ]
