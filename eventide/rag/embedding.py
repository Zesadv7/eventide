"""Offline and OpenAI-compatible embedding providers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from array import array
from dataclasses import dataclass, replace
from typing import Any, Protocol

from eventide.config import Settings

HASH_ALGORITHM_VERSION = "hash-sha256-v1"
OPENAI_ALGORITHM_VERSION = "openai-compatible-v1"


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    provider: str
    model: str
    base_url: str
    algorithm_version: str
    dimension: int | None

    def resolved(self, dimension: int) -> EmbeddingProfile:
        return replace(self, dimension=dimension)

    def fingerprint(self) -> str:
        if self.dimension is None:
            raise ValueError("Embedding dimension is not resolved")
        payload = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "base_url": self.base_url,
                "algorithm_version": self.algorithm_version,
                "dimension": self.dimension,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class HashEmbedder:
    """Deterministic local vectors for offline tests and mechanism validation only.

    This hashing trick has no learned semantic knowledge. Real semantic retrieval
    requires a suitable configured embedding model.
    """

    def __init__(self, dimension: int = 256):
        if dimension <= 0:
            raise ValueError("Embedding dimension must be positive")
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in (item for item in re.split(r"\W+", text.lower()) if item):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                bucket = int.from_bytes(digest, "big") % self.dimension
                vector[bucket] += 1.0
            norm = math.sqrt(sum(value * value for value in vector))
            vectors.append([value / norm for value in vector] if norm else vector)
        return vectors


class OpenAICompatibleEmbedder:
    """Lazy adapter for OpenAI-compatible embedding endpoints."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str | None,
        client: Any | None = None,
    ):
        if not model:
            raise ValueError("EVENTIDE_EMBEDDING_MODEL is required for API embeddings")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "No embedding API key configured. Set EVENTIDE_EMBEDDING_API_KEY "
                    "or EVENTIDE_API_KEY."
                )
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._get_client().embeddings.create(model=self.model, input=texts)
        data = list(response.data)
        if len(data) != len(texts):
            raise RuntimeError("Embedding response count does not match the request")
        ordered = sorted(data, key=lambda item: int(getattr(item, "index", 0)))
        vectors = [[float(value) for value in item.embedding] for item in ordered]
        if any(not vector for vector in vectors):
            raise RuntimeError("Embedding response contains an empty vector")
        return vectors


def vector_to_blob(vector: list[float]) -> bytes:
    values = array("f", vector)
    if sys.byteorder != "little":  # pragma: no cover - supported indexes are local/rebuildable
        values.byteswap()
    return values.tobytes()


def vector_from_blob(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":  # pragma: no cover - supported indexes are local/rebuildable
        values.byteswap()
    return list(values)


def provider_from_settings(settings: Settings) -> tuple[EmbeddingProvider, EmbeddingProfile]:
    """Construct the configured provider without network access."""
    provider = settings.embedding_provider.strip().lower().replace("-", "_")
    if provider == "hash":
        hash_embedder = HashEmbedder(settings.embedding_dim)
        return hash_embedder, EmbeddingProfile(
            provider="hash",
            model="hash-v1",
            base_url="local",
            algorithm_version=HASH_ALGORITHM_VERSION,
            dimension=hash_embedder.dimension,
        )
    if provider != "openai_compatible":
        raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")
    model = settings.embedding_model or ""
    api_embedder = OpenAICompatibleEmbedder(
        model=model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
    )
    return api_embedder, EmbeddingProfile(
        provider="openai_compatible",
        model=model,
        base_url=settings.embedding_base_url or "sdk-default",
        algorithm_version=OPENAI_ALGORITHM_VERSION,
        dimension=None,
    )
