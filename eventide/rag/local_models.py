"""Explicitly prepared, content-pinned local retrieval models; no download path."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from eventide.config import Settings
from eventide.rag.embedding import EmbeddingProfile

MANIFEST = "eventide-model.json"
LIBRARY_VERSION = "6.1.0"
EMBED_BATCH_SIZE = 16
RERANK_BATCH_SIZE = 8
MAX_RERANK_CANDIDATES = 20
MAX_MODEL_BYTES = 2 * 1024**3
MAX_CONFIG_BYTES = 4 * 1024**2
_MODULES = frozenset({
    "sentence_transformers.models.Transformer",
    "sentence_transformers.models.Pooling",
    "sentence_transformers.models.Normalize",
})
ModelKind = Literal["embedding", "reranker"]


class LocalModelError(RuntimeError):
    """A stable diagnostic code; callers must not invent semantic scores."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(path: Path) -> Any:
    with path.open("rb") as stream:
        body = stream.read(MAX_CONFIG_BYTES + 1)
    if len(body) > MAX_CONFIG_BYTES:
        raise LocalModelError("local_model_config_too_large")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise LocalModelError("local_model_invalid_config") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _root(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise LocalModelError("local_model_absolute_directory_required")
    try:
        if not path.is_dir() or path.resolve(strict=True) != path:
            raise LocalModelError("local_model_missing_or_linked_directory")
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, LocalModelError):
            raise
        raise LocalModelError("local_model_missing_or_linked_directory") from exc
    return path


def _safe_relative(value: str, *, empty: bool = False) -> bool:
    if not value:
        return empty
    path = PurePosixPath(value)
    return (
        not path.is_absolute() and ".." not in path.parts and "\\" not in value
        and path.as_posix() == value and value != "."
    )


def _config_code(value: Any) -> None:
    if isinstance(value, dict):
        prohibited = {
            "auto_map", "trust_remote_code", "model_name_or_path", "tokenizer_name_or_path",
            "local_files_only", "cache_dir", "token", "use_auth_token",
        }
        if prohibited & value.keys():
            raise LocalModelError("local_model_custom_code_or_redirect_rejected")
        for child in value.values():
            _config_code(child)
    elif isinstance(value, list):
        for child in value:
            _config_code(child)


def _files(root: Path, kind: ModelKind) -> dict[str, dict[str, str | int]]:
    files: dict[str, dict[str, str | int]] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise LocalModelError("local_model_link_rejected")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or path.stat().st_nlink != 1 or not _safe_relative(relative):
            raise LocalModelError("local_model_invalid_file")
        if relative == MANIFEST:
            continue
        if path.suffix not in {".json", ".txt", ".safetensors"}:
            raise LocalModelError("local_model_unsupported_file")
        if len(files) >= 256:
            raise LocalModelError("local_model_too_many_files")
        size = 0
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while body := stream.read(1024 * 1024):
                size += len(body)
                total += len(body)
                if total > MAX_MODEL_BYTES:
                    raise LocalModelError("local_model_too_large")
                digest.update(body)
        files[relative] = {"size": size, "sha256": digest.hexdigest()}
        if path.suffix == ".json" and path.name not in {
            "tokenizer.json", "special_tokens_map.json",
        }:
            _config_code(_json(path))
    if not {"config.json", "model.safetensors", "tokenizer_config.json"}.issubset(files):
        raise LocalModelError("local_model_required_files_missing")
    if kind == "embedding":
        if "modules.json" not in files:
            raise LocalModelError("local_model_modules_missing")
        modules = _json(root / "modules.json")
        if not isinstance(modules, list) or not modules:
            raise LocalModelError("local_model_invalid_modules")
        for module in modules:
            if (
                not isinstance(module, dict) or module.get("type") not in _MODULES
                or not isinstance(module.get("path"), str)
                or not _safe_relative(module["path"], empty=True)
            ):
                raise LocalModelError("local_model_custom_code_or_redirect_rejected")
    elif "modules.json" in files:
        # First release supports a standard Transformers sequence classifier only.
        raise LocalModelError("local_model_custom_code_or_redirect_rejected")
    return files


@dataclass(frozen=True, slots=True)
class PreparedModel:
    path: Path
    kind: ModelKind
    revision: str
    fingerprint: str

    def verify(self) -> None:
        current = load_local_model(self.path, kind=self.kind)
        if current != self:
            raise LocalModelError("local_model_changed")


def load_local_model(path: Path, *, kind: ModelKind) -> PreparedModel:
    """Validate a prepared directory without importing any ML dependencies."""
    root = _root(path)
    manifest = root / MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise LocalModelError("local_model_manifest_missing")
    value = _json(manifest)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "kind", "revision", "files", "fingerprint"}
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["kind"] != kind or kind not in {"embedding", "reranker"}
        or not isinstance(value["revision"], str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["revision"]) is None
    ):
        raise LocalModelError("local_model_invalid_manifest")
    expected = {key: value[key] for key in ("schema_version", "kind", "revision", "files")}
    if _digest(expected) != value["fingerprint"] or _files(root, kind) != value["files"]:
        raise LocalModelError("local_model_changed")
    return PreparedModel(root, kind, value["revision"], value["fingerprint"])


def prepare_local_model(
    source: Path, destination: Path, *, kind: ModelKind, revision: str,
) -> PreparedModel:
    """Copy explicitly downloaded safe files into a new immutable-version directory.

    This function never downloads, imports model code, installs dependencies, or
    overwrites an existing version. A revision must be a fixed commit/content SHA.
    """
    root = _root(source)
    if (
        kind not in {"embedding", "reranker"}
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)
    ):
        raise LocalModelError("local_model_fixed_revision_required")
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise LocalModelError("local_model_new_absolute_destination_required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _root(destination.parent)
    files = _files(root, kind)
    value = {"schema_version": 1, "kind": kind, "revision": revision, "files": files}
    staged = Path(tempfile.mkdtemp(prefix=".eventide-model-", dir=destination.parent))
    try:
        for relative in files:
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            limit = int(files[relative]["size"])
            with (root / relative).open("rb") as incoming, target.open("xb") as outgoing:
                copied = 0
                while body := incoming.read(min(1024 * 1024, limit - copied + 1)):
                    copied += len(body)
                    if copied > limit:
                        raise LocalModelError("local_model_changed")
                    outgoing.write(body)
        (staged / MANIFEST).write_text(json.dumps(
            {**value, "fingerprint": _digest(value)}, ensure_ascii=False, sort_keys=True,
        ), encoding="utf-8")
        load_local_model(staged, kind=kind)
        if destination.exists() or destination.is_symlink():
            raise LocalModelError("local_model_destination_exists")
        staged.rename(destination)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return load_local_model(destination, kind=kind)


def _library() -> Any:
    try:
        if version("sentence-transformers") != LIBRARY_VERSION:
            raise LocalModelError("local_model_dependency_version_mismatch")
        return importlib.import_module("sentence_transformers")
    except (ImportError, PackageNotFoundError) as exc:
        raise LocalModelError("local_model_dependency_missing") from exc


class LocalSemanticEmbedder:
    def __init__(self, path: Path):
        self.prepared = load_local_model(path, kind="embedding")
        config_path = self.prepared.path / "config_sentence_transformers.json"
        config = _json(config_path) if config_path.exists() else {}
        if not isinstance(config, dict) or not isinstance(config.get("prompts", {}), dict):
            raise LocalModelError("local_model_invalid_prompts")
        prompts = config.get("prompts", {})
        self._prompts = {role: prompts.get(role, "") for role in ("query", "document")}
        if any(not isinstance(prompt, str) for prompt in self._prompts.values()):
            raise LocalModelError("local_model_invalid_prompts")
        self._model: Any = None

    def profile(self) -> EmbeddingProfile:
        return EmbeddingProfile(
            provider="local_semantic", model=self.prepared.fingerprint, base_url="local",
            algorithm_version=(
                f"sentence-transformers-{LIBRARY_VERSION}-role-prompts-normalized-float32-v2"
            ),
            dimension=None,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode indexed documents with the immutable package's document prompt."""
        return self._embed(texts, prompt=self._prompts["document"])

    def embed_query(self, query: str) -> list[float]:
        """Encode search text with the same package's query instruction."""
        return self._embed([query], prompt=self._prompts["query"])[0]

    def _embed(self, texts: list[str], *, prompt: str) -> list[list[float]]:
        if not texts:
            return []
        self.prepared.verify()
        try:
            if self._model is None:
                self._model = _library().SentenceTransformer(
                    str(self.prepared.path), device="cpu", local_files_only=True,
                    trust_remote_code=False, token=False, model_kwargs={"use_safetensors": True},
                )
            vectors = []
            for start in range(0, len(texts), EMBED_BATCH_SIZE):
                batch = texts[start:start + EMBED_BATCH_SIZE]
                encoded = self._model.encode(
                    batch, batch_size=EMBED_BATCH_SIZE, show_progress_bar=False, prompt=prompt,
                    normalize_embeddings=True, precision="float32", convert_to_numpy=True,
                )
                values = [[float(value) for value in row] for row in encoded]
                if len(values) != len(batch) or any(
                    not row or any(not math.isfinite(value) for value in row) for row in values
                ):
                    raise LocalModelError("local_embedding_invalid_response")
                vectors.extend(values)
            if len({len(row) for row in vectors}) != 1:
                raise LocalModelError("local_embedding_invalid_response")
            self.prepared.verify()
            return vectors
        except LocalModelError:
            raise
        except Exception as exc:
            raise LocalModelError("local_embedding_failed") from exc


class LocalCrossEncoder:
    def __init__(self, path: Path):
        self.path = path
        self.prepared: PreparedModel | None = None
        self._model: Any = None

    def score(self, query: str, passages: list[str]) -> list[float]:
        if len(passages) > MAX_RERANK_CANDIDATES:
            raise ValueError("At most 20 candidates may be reranked")
        if not passages:
            return []
        if self.prepared is None:
            self.prepared = load_local_model(self.path, kind="reranker")
        self.prepared.verify()
        try:
            if self._model is None:
                self._model = _library().CrossEncoder(
                    str(self.prepared.path), device="cpu", local_files_only=True,
                    trust_remote_code=False, token=False, max_length=512,
                    model_kwargs={"use_safetensors": True},
                )
            values = self._model.predict(
                [(query, text) for text in passages], batch_size=RERANK_BATCH_SIZE,
                show_progress_bar=False, convert_to_numpy=True,
            )
            scores = [float(value) for value in values]
            if len(scores) != len(passages) or any(not math.isfinite(value) for value in scores):
                raise LocalModelError("local_reranker_invalid_response")
            self.prepared.verify()
            return scores
        except LocalModelError:
            raise
        except Exception as exc:
            raise LocalModelError("local_reranker_failed") from exc


def reranker_from_settings(settings: Settings) -> LocalCrossEncoder | None:
    """Opt in without loading dependencies or silently discarding a missing model."""
    return LocalCrossEncoder(settings.reranker_model_path) if settings.reranker_model_path else None
