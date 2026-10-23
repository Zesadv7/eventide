"""SQLite-backed Workspace knowledge index."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import stat as stat_module
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

from eventide.rag.chunker import (
    Chunk,
    chunk_document,
    chunking_fingerprint,
    chunking_identity,
)
from eventide.rag.embedding import (
    HASH_ALGORITHM_VERSION,
    EmbeddingProfile,
    EmbeddingProvider,
    HashEmbedder,
    vector_from_blob,
    vector_to_blob,
)
from eventide.rag.source_selection import (
    SourceScope,
    exclusion_reason,
    source_selection_fingerprint,
    source_selection_identity,
)
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
    structured_files: int = 0
    structure_fallbacks: dict[str, int] = field(default_factory=dict)
    chunking_fingerprint: str = ""
    generation: int = 0
    source_scope: SourceScope = "code"
    source_selection_fingerprint: str = ""
    skipped_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    id: int
    file: str
    start_line: int
    end_line: int
    heading: str
    text: str
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class SparseCandidate:
    chunk: IndexedChunk
    rank: int
    bm25: float


@dataclass(frozen=True, slots=True)
class DenseCandidate:
    chunk: IndexedChunk
    rank: int
    similarity: float


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
    structured_files: int = 0
    structure_fallbacks: tuple[tuple[str, int], ...] = ()
    skipped_reasons: tuple[tuple[str, int], ...] = ()


class IndexStore:
    """Own the rebuildable knowledge index for one canonical Git Workspace."""

    def __init__(self, workspace: Path, *, index_dir: Path | None = None):
        root, git_root = canonical_workspace(workspace)
        if git_root is None:
            raise ValueError("RAG indexing requires a Git workspace")
        self.workspace = root
        self._workspace_identity = self._identity(root)
        self.index_dir = index_dir.absolute() if index_dir is not None else None
        self.path = ((self.index_dir / "index.sqlite") if self.index_dir is not None
                     else root / INDEX_RELATIVE_PATH)
        self._bound_path = self.path
        self._snapshot: sqlite3.Connection | None = None

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        info = path.stat()
        return info.st_dev, info.st_ino

    def _validate_workspace(self) -> None:
        try:
            if (
                self.workspace.resolve(strict=True) == self.workspace
                and self.workspace.is_dir()
                and self._identity(self.workspace) == self._workspace_identity
            ):
                return
        except (OSError, RuntimeError):
            pass
        raise ValueError("Knowledge index Workspace path changed; reopen the Workspace")

    def _validate_index_path(self) -> None:
        """Reject redirected index artifacts before SQLite can open or create them."""
        self._validate_workspace()
        if self.path != self._bound_path:
            raise ValueError(
                "Knowledge index path must remain inside its Workspace or bound index directory"
            )
        if self.path.parent.resolve(strict=False) != self.path.parent:
            raise ValueError("Knowledge index path contains a link or unsupported file type")
        paths = [self.path.parent, self.path]
        paths.extend(self.path.with_name(self.path.name + suffix) for suffix in (
            "-wal", "-shm", "-journal",
        ))
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            directory = path == self.path.parent
            valid_type = stat_module.S_ISDIR if directory else stat_module.S_ISREG
            if (
                not valid_type(info.st_mode)
                or path.resolve(strict=True) != path
                or (not directory and info.st_nlink != 1)
            ):
                raise ValueError("Knowledge index path contains a link or unsupported file type")

    def _index_identity(self) -> tuple[tuple[int, int], tuple[int, int]]:
        self._validate_index_path()
        return self._identity(self.path.parent), self._identity(self.path)

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        self._validate_index_path()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise ValueError("No knowledge index; build it in the Web knowledge index page")
        self._validate_index_path()
        # Create an empty regular file first so even a new SQLite connection can
        # be checked against a known inode before its first SQL statement.
        if create:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        identity = self._index_identity()
        target = str(self.path) if create else f"{self.path.as_uri()}?mode=ro"
        connection = sqlite3.connect(target, uri=not create, timeout=5, isolation_level=None)
        try:
            if self._index_identity() != identity:
                raise RuntimeError("Knowledge index path changed while opening; retry")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if create:
                connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def snapshot(self) -> Iterator[IndexStore]:
        """Pin metadata and both retrieval paths to one WAL read transaction."""
        self._validate_workspace()
        reader = IndexStore(self.workspace, index_dir=self.index_dir)
        connection = reader._connect(create=False)
        try:
            connection.execute("BEGIN")
            # BEGIN alone does not establish SQLite's snapshot; the first read does.
            self._generation(connection)
            reader._snapshot = connection
            yield reader
        finally:
            reader._snapshot = None
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._snapshot or self._connect(create=False)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {1, 2}:
                raise ValueError(f"Unsupported knowledge index version: {version}")
            yield connection
        finally:
            if self._snapshot is None:
                connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1, 2}:
            raise ValueError(f"Unsupported knowledge index version: {version}")
        if version in {1, 2}:
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

    def _scan(
        self, *, include_chunks: bool, code_structure: bool = False,
        source_scope: SourceScope = "code",
    ) -> _Scan:
        self._validate_workspace()
        sources: list[_SourceFile] = []
        signature: list[tuple[str, str]] = []
        skipped_reasons: Counter[str] = Counter()
        structured_files = 0
        structure_fallbacks: dict[str, int] = {}
        for raw_path in self._git_paths():
            relative = PurePosixPath(raw_path).as_posix()
            parts = PurePosixPath(relative).parts
            if not parts or parts[0] in {".git", ".eventide"}:
                continue
            if source_scope == "code" and (reason := exclusion_reason(relative)):
                skipped_reasons[reason] += 1
                signature.append((relative, reason))
                continue
            candidate = self.workspace / Path(*parts)
            try:
                if (
                    relative != raw_path
                    or PurePosixPath(relative).is_absolute()
                    or ".." in parts
                    or "\\" in raw_path
                    or candidate.resolve(strict=True) != candidate
                    or not candidate.is_file()
                ):
                    skipped_reasons["path_boundary"] += 1
                    signature.append((relative, "skipped"))
                    continue
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                with os.fdopen(os.open(candidate, flags), "rb") as stream:
                    stat = os.fstat(stream.fileno())
                    if (
                        not stat_module.S_ISREG(stat.st_mode)
                        or candidate.resolve(strict=True) != candidate
                        or self._identity(candidate) != (stat.st_dev, stat.st_ino)
                    ):
                        skipped_reasons["path_boundary"] += 1
                        signature.append((relative, "skipped"))
                        continue
                    body = (
                        stream.read(MAX_SOURCE_BYTES + 1)
                        if stat.st_size <= MAX_SOURCE_BYTES else b""
                    )
                    if stat.st_size > MAX_SOURCE_BYTES or len(body) > MAX_SOURCE_BYTES:
                        skipped_reasons["too_large"] += 1
                        size = max(stat.st_size, len(body))
                        signature.append((relative, f"large:{stat.st_mtime_ns}:{size}"))
                        continue
            except (OSError, RuntimeError):
                skipped_reasons["unreadable"] += 1
                signature.append((relative, "unreadable"))
                continue
            content_hash = hashlib.sha256(body).hexdigest()
            if b"\0" in body:
                skipped_reasons["binary"] += 1
                signature.append((relative, f"binary:{content_hash}"))
                continue
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                skipped_reasons["non_utf8"] += 1
                signature.append((relative, f"non-utf8:{content_hash}"))
                continue
            signature.append((relative, content_hash))
            if source_scope == "code" and (reason := exclusion_reason(relative, header=text)):
                skipped_reasons[reason] += 1
                continue
            chunks: tuple[Chunk, ...] = ()
            if include_chunks:
                document = chunk_document(relative, text, code_structure=code_structure)
                chunks = document.chunks
                structured_files += int(document.structured)
                if document.fallback_reason:
                    reason = document.fallback_reason
                    structure_fallbacks[reason] = structure_fallbacks.get(reason, 0) + 1
            sources.append(
                _SourceFile(
                    path=relative,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                    content_hash=content_hash,
                    chunks=chunks,
                )
            )
        self._validate_workspace()
        return _Scan(
            tuple(sources),
            sum(skipped_reasons.values()),
            tuple(signature),
            structured_files,
            tuple(sorted(structure_fallbacks.items())),
            tuple(sorted(skipped_reasons.items())),
        )

    @staticmethod
    def _generation(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'generation'"
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _meta(connection: sqlite3.Connection) -> dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM meta")
        }

    @staticmethod
    def _embed_chunks(
        sources: list[_SourceFile], embedder: EmbeddingProvider
    ) -> tuple[dict[tuple[str, int], bytes], int | None]:
        keys: list[tuple[str, int]] = []
        texts: list[str] = []
        for source in sources:
            for ordinal, chunk in enumerate(source.chunks):
                keys.append((source.path, ordinal))
                texts.append(chunk.text)
        encoded: dict[tuple[str, int], bytes] = {}
        dimension: int | None = None
        for start in range(0, len(texts), 128):
            batch = texts[start : start + 128]
            vectors = embedder.embed(batch)
            if len(vectors) != len(batch):
                raise RuntimeError("Embedding response count does not match the request")
            for key, vector in zip(keys[start : start + 128], vectors, strict=True):
                if not vector or any(not math.isfinite(value) for value in vector):
                    raise RuntimeError("Embedding response contains an invalid vector")
                if dimension is None:
                    dimension = len(vector)
                elif len(vector) != dimension:
                    raise RuntimeError("Embedding dimension changed during build")
                encoded[key] = vector_to_blob(vector)
        return encoded, dimension

    def build(
        self,
        *,
        force: bool = False,
        embedder: EmbeddingProvider | None = None,
        profile: EmbeddingProfile | None = None,
        code_structure: bool = False,
        source_scope: str = "code",
        check_cancelled: Callable[[], None] | None = None,
    ) -> BuildStats:
        """Incrementally rebuild changed files and commit the new snapshot atomically."""
        started = time.perf_counter()
        if check_cancelled:
            check_cancelled()
        selection_profile = source_selection_identity(source_scope)
        selected_scope = cast(SourceScope, source_scope)
        selection_digest = source_selection_fingerprint(selection_profile)
        chunking_profile = chunking_identity(code_structure=code_structure)
        chunking_digest = chunking_fingerprint(chunking_profile)
        if embedder is None:
            default_embedder = HashEmbedder()
            embedder = default_embedder
            profile = EmbeddingProfile(
                provider="hash",
                model="hash-v1",
                base_url="local",
                algorithm_version=HASH_ALGORITHM_VERSION,
                dimension=default_embedder.dimension,
            )
        if profile is None:
            raise ValueError("Embedding profile is required with a custom provider")
        connection = self._connect(create=True)
        try:
            index_identity = self._index_identity()
            self._ensure_schema(connection)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            generation = self._generation(connection)
            meta = self._meta(connection)
            existing = {
                str(row["path"]): str(row["content_hash"])
                for row in connection.execute("SELECT path, content_hash FROM files")
            }
            stored_chunking = meta.get("chunking_fingerprint")
            stored_chunking_profile = meta.get("chunking_profile")
            if not force and (
                (stored_chunking is None) != (stored_chunking_profile is None)
                or (
                    stored_chunking_profile is not None
                    and chunking_fingerprint(stored_chunking_profile) != stored_chunking
                )
            ):
                raise ValueError("Knowledge index chunking fingerprint is invalid; rebuild --force")
            if (
                (existing or stored_chunking is not None)
                and not force
                and (
                    (stored_chunking is not None and stored_chunking != chunking_digest)
                    or stored_chunking is None
                )
            ):
                raise ValueError("Chunking configuration changed; rebuild with --force")
            stored_selection = meta.get("source_selection_fingerprint")
            stored_selection_profile = meta.get("source_selection_profile")
            if not force and (
                (stored_selection is None) != (stored_selection_profile is None)
                or (
                    stored_selection_profile is not None
                    and source_selection_fingerprint(stored_selection_profile) != stored_selection
                )
            ):
                raise ValueError("Source selection fingerprint is invalid; rebuild with --force")
            if (existing or stored_selection is not None) and not force and (
                stored_selection != selection_digest
            ):
                raise ValueError("Source selection configuration changed; rebuild with --force")
            chunk_total, null_total = connection.execute(
                "SELECT COUNT(*), SUM(CASE WHEN vec IS NULL THEN 1 ELSE 0 END) FROM chunks"
            ).fetchone()
            chunk_total = int(chunk_total)
            null_total = int(null_total or 0)
            if version == 2 and not force:
                self._validated_embedding_profile(meta)
            stored_dimension = (
                int(meta["embedding_dimension"])
                if meta.get("embedding_dimension")
                else None
            )
            stored_fields = (
                meta.get("embedding_provider"),
                meta.get("embedding_model"),
                meta.get("embedding_base_url"),
                meta.get("embedding_algorithm"),
            )
            requested_fields = (
                profile.provider,
                profile.model,
                profile.base_url,
                profile.algorithm_version,
            )
            if version == 1 and 0 < null_total < chunk_total and not force:
                raise ValueError("Knowledge index has mixed vector state; rebuild with --force")
            if version == 1 and null_total != chunk_total and chunk_total and not force:
                raise ValueError("Knowledge index v1 has unexpected vectors; rebuild with --force")
            if version == 2:
                if null_total and not force:
                    raise ValueError("Knowledge index has missing vectors; rebuild with --force")
                if stored_fields != requested_fields and not force:
                    raise ValueError("Embedding configuration changed; rebuild with --force")
                if (
                    profile.dimension is not None
                    and stored_dimension != profile.dimension
                    and not force
                ):
                    raise ValueError("Embedding configuration changed; rebuild with --force")
            scan = self._scan(
                include_chunks=True, code_structure=code_structure, source_scope=selected_scope,
            )
            current = {source.path: source for source in scan.sources}
            changed = [
                source
                for source in scan.sources
                if force or existing.get(source.path) != source.content_hash
            ]
            deleted = set(existing) - set(current)
            full_embedding = force or version == 1
            embedding_sources = list(scan.sources) if full_embedding else changed
            vectors, observed_dimension = self._embed_chunks(embedding_sources, embedder)
            dimension = observed_dimension or profile.dimension or stored_dimension
            if dimension is None:
                raise ValueError("Cannot determine embedding dimension from an empty corpus")
            if profile.dimension is not None and dimension != profile.dimension:
                raise RuntimeError("Embedding provider returned an unexpected dimension")
            if stored_dimension is not None and not force and dimension != stored_dimension:
                raise ValueError("Embedding dimension changed; rebuild with --force")
            resolved_profile = profile.resolved(dimension)
            if self._index_identity() != index_identity:
                raise RuntimeError("Knowledge index path changed during build; retry")
            if check_cancelled:
                check_cancelled()
            connection.execute("BEGIN IMMEDIATE")
            try:
                if check_cancelled:
                    check_cancelled()
                if self._generation(connection) != generation:
                    raise RuntimeError("Knowledge index changed during build; retry")
                verified = self._scan(include_chunks=False, source_scope=selected_scope)
                if verified.signature != scan.signature:
                    raise RuntimeError("Workspace changed during index build; retry")
                if chunking_identity(code_structure=code_structure) != chunking_profile:
                    raise RuntimeError("Chunking configuration changed during build; retry")
                if self._index_identity() != index_identity:
                    raise RuntimeError("Knowledge index path changed during build; retry")
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
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                source.path,
                                ordinal,
                                chunk.start_line,
                                chunk.end_line,
                                chunk.heading,
                                chunk.text,
                                vectors[(source.path, ordinal)],
                            )
                            for ordinal, chunk in enumerate(source.chunks)
                        ],
                    )
                if full_embedding and not force:
                    connection.executemany(
                        "UPDATE chunks SET vec = ? WHERE file = ? AND ordinal = ?",
                        [
                            (blob, path, ordinal)
                            for (path, ordinal), blob in vectors.items()
                        ],
                    )
                embedding_meta = {
                    "embedding_provider": resolved_profile.provider,
                    "embedding_model": resolved_profile.model,
                    "embedding_base_url": resolved_profile.base_url,
                    "embedding_algorithm": resolved_profile.algorithm_version,
                    "embedding_dimension": str(dimension),
                    "embedding_fingerprint": resolved_profile.fingerprint(),
                    "chunking_profile": chunking_profile,
                    "chunking_fingerprint": chunking_digest,
                    "source_selection_profile": selection_profile,
                    "source_selection_fingerprint": selection_digest,
                }
                connection.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    list(embedding_meta.items()),
                )
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('generation', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(generation + 1),),
                )
                connection.execute("PRAGMA user_version = 2")
                chunk_count = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                if check_cancelled:
                    check_cancelled()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return BuildStats(
            files=len(scan.sources),
            rebuilt_files=len(changed),
            deleted_files=len(existing) if force else len(deleted),
            chunks=chunk_count,
            skipped=scan.skipped,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            structured_files=scan.structured_files,
            structure_fallbacks=dict(scan.structure_fallbacks),
            chunking_fingerprint=chunking_digest,
            generation=generation + 1,
            source_scope=selected_scope,
            source_selection_fingerprint=selection_digest,
            skipped_reasons=dict(scan.skipped_reasons),
        )

    def sparse_search(self, query: str, *, limit: int) -> list[SparseCandidate]:
        """Return deterministic BM25 candidates from the trigram FTS index."""
        cleaned = query.strip()[:256]
        if not cleaned:
            raise ValueError("Search query must not be empty")
        terms = [term for term in cleaned.split() if term]
        match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        try:
            with self._reader() as connection:
                rows = connection.execute(
                    "SELECT chunks.id, chunks.file, chunks.start_line, chunks.end_line, "
                    "chunks.heading, chunks.text, files.content_hash, bm25(fts) AS score "
                    "FROM fts JOIN chunks ON chunks.id = fts.rowid "
                    "JOIN files ON files.path = chunks.file "
                    "WHERE fts MATCH ? ORDER BY score ASC, chunks.id ASC LIMIT ?",
                    (match, limit),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Invalid knowledge search query: {query}") from exc
        return [
            SparseCandidate(
                chunk=IndexedChunk(
                    id=int(row["id"]),
                    file=str(row["file"]),
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    heading=str(row["heading"]),
                    text=str(row["text"]),
                    content_hash=str(row["content_hash"]),
                ),
                rank=rank,
                bm25=float(row["score"]),
            )
            for rank, row in enumerate(rows, 1)
        ]

    def generation(self) -> int:
        """Read the current published generation without holding a transaction during inference."""
        with self._reader() as connection:
            return self._generation(connection)

    def retrieval_identity(self) -> tuple[int, str | None]:
        """Read the evidence identity from the caller's pinned read snapshot."""
        with self._reader() as connection:
            meta = self._meta(connection)
            fingerprint = (
                self._validated_embedding_profile(meta).fingerprint()
                if int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
                else None
            )
            return self._generation(connection), fingerprint

    def file_freshness(self, relative: str, content_hash: str) -> str:
        """Check one hit against bounded current bytes without following source links."""
        self._validate_workspace()
        parts = PurePosixPath(relative).parts
        if (not parts or PurePosixPath(relative).is_absolute() or ".." in parts
                or "\\" in relative or PurePosixPath(relative).as_posix() != relative):
            return "unsafe"
        candidate = self.workspace / Path(*parts)
        try:
            if (candidate.resolve(strict=True) != candidate
                    or not stat_module.S_ISREG(candidate.lstat().st_mode)):
                return "unsafe"
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(candidate, flags), "rb") as stream:
                before = os.fstat(stream.fileno())
                if (not stat_module.S_ISREG(before.st_mode)
                        or candidate.resolve(strict=True) != candidate
                        or self._identity(candidate) != (before.st_dev, before.st_ino)):
                    return "unsafe"
                if before.st_size > MAX_SOURCE_BYTES:
                    return "too_large"
                body = stream.read(MAX_SOURCE_BYTES + 1)
                if len(body) > MAX_SOURCE_BYTES:
                    return "too_large"
                after = os.fstat(stream.fileno())
                if (candidate.resolve(strict=True) != candidate
                        or self._identity(candidate) != (after.st_dev, after.st_ino)):
                    return "unsafe"
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    return "changed"
        except FileNotFoundError:
            return "deleted"
        except RuntimeError:
            return "unsafe"
        except OSError:
            return "unreadable"
        self._validate_workspace()
        return "fresh" if hashlib.sha256(body).hexdigest() == content_hash else "changed"

    def embedding_profile(self) -> EmbeddingProfile:
        """Read and verify the vector identity stored with the index."""
        with self._reader() as connection:
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 2:
                raise ValueError(
                    "Knowledge index has no vectors; build it in the Web knowledge index page"
                )
            meta = self._meta(connection)
        return self._validated_embedding_profile(meta)

    @staticmethod
    def _validated_embedding_profile(meta: dict[str, str]) -> EmbeddingProfile:
        required = {
            "embedding_provider",
            "embedding_model",
            "embedding_base_url",
            "embedding_algorithm",
            "embedding_dimension",
            "embedding_fingerprint",
        }
        if not required.issubset(meta):
            raise ValueError("Knowledge index embedding metadata is incomplete; rebuild --force")
        profile = EmbeddingProfile(
            provider=meta["embedding_provider"],
            model=meta["embedding_model"],
            base_url=meta["embedding_base_url"],
            algorithm_version=meta["embedding_algorithm"],
            dimension=int(meta["embedding_dimension"]),
        )
        if profile.fingerprint() != meta["embedding_fingerprint"]:
            raise ValueError("Knowledge index embedding fingerprint is invalid; rebuild --force")
        return profile

    def validate_profile(self, requested: EmbeddingProfile) -> EmbeddingProfile:
        stored = self.embedding_profile()
        fields_match = (
            stored.provider,
            stored.model,
            stored.base_url,
            stored.algorithm_version,
        ) == (
            requested.provider,
            requested.model,
            requested.base_url,
            requested.algorithm_version,
        )
        dimension_matches = requested.dimension is None or requested.dimension == stored.dimension
        if not fields_match or not dimension_matches:
            raise ValueError("Embedding configuration changed; rebuild with --force")
        return stored

    def dense_search(self, vector: list[float], *, limit: int) -> list[DenseCandidate]:
        """Rank every stored float32 vector by cosine similarity."""
        if not vector or not any(vector):
            return []
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT chunks.id, file, start_line, end_line, heading, text, vec, "
                "files.content_hash FROM chunks JOIN files ON files.path = chunks.file "
                "WHERE vec IS NOT NULL"
            ).fetchall()
        query_norm = math.sqrt(sum(value * value for value in vector))
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            stored = vector_from_blob(bytes(row["vec"]))
            if len(stored) != len(vector):
                raise ValueError(
                    "Knowledge index vector dimension is inconsistent; rebuild --force"
                )
            stored_norm = math.sqrt(sum(value * value for value in stored))
            similarity = (
                sum(left * right for left, right in zip(vector, stored, strict=True))
                / (query_norm * stored_norm)
                if stored_norm
                else 0.0
            )
            scored.append((similarity, row))
        scored.sort(key=lambda item: (-item[0], int(item[1]["id"])))
        return [
            DenseCandidate(
                chunk=IndexedChunk(
                    id=int(row["id"]),
                    file=str(row["file"]),
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    heading=str(row["heading"]),
                    text=str(row["text"]),
                    content_hash=str(row["content_hash"]),
                ),
                rank=rank,
                similarity=similarity,
            )
            for rank, (similarity, row) in enumerate(scored[:limit], 1)
        ]
