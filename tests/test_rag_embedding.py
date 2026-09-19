"""Offline embedding determinism and adapter validation."""

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from eventide.rag.embedding import (
    EmbeddingProfile,
    HashEmbedder,
    OpenAICompatibleEmbedder,
    provider_from_settings,
    vector_from_blob,
    vector_to_blob,
)
from tests.test_runtime import settings_for


def test_hash_embedder_is_deterministic_normalized_and_distinct() -> None:
    embedder = HashEmbedder(64)
    first, repeated, different = embedder.embed(["Alpha alpha beta", "Alpha alpha beta", "gamma"])

    assert first == repeated
    assert first != different
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0)
    assert embedder.embed([""])[0] == [0.0] * 64


def test_vector_blob_round_trip_uses_float32() -> None:
    restored = vector_from_blob(vector_to_blob([0.25, -0.5, 1.0]))
    assert restored == pytest.approx([0.25, -0.5, 1.0])


def test_openai_compatible_adapter_uses_injected_client_only() -> None:
    calls = []

    class FakeEmbeddings:
        def create(self, *, model, input):
            calls.append((model, input))
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                    SimpleNamespace(index=0, embedding=[1.0, 0.0]),
                ]
            )

    client = SimpleNamespace(embeddings=FakeEmbeddings())
    embedder = OpenAICompatibleEmbedder(
        model="fake-embedding", api_key=None, base_url=None, client=client
    )

    assert embedder.embed(["one", "two"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert calls == [("fake-embedding", ["one", "two"])]


def test_openai_compatible_adapter_rejects_bad_counts() -> None:
    client = SimpleNamespace(
        embeddings=SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[1.0])]
            )
        )
    )
    embedder = OpenAICompatibleEmbedder(
        model="fake", api_key=None, base_url=None, client=client
    )
    with pytest.raises(RuntimeError, match="count"):
        embedder.embed(["one", "two"])


def test_embedding_validation_failures_are_offline(isolated_workspace) -> None:
    with pytest.raises(ValueError, match="positive"):
        HashEmbedder(0)
    unresolved = EmbeddingProfile("hash", "hash-v1", "local", "v1", None)
    with pytest.raises(ValueError, match="not resolved"):
        unresolved.fingerprint()
    missing_key = OpenAICompatibleEmbedder(
        model="fake", api_key=None, base_url=None
    )
    assert missing_key.embed([]) == []
    with pytest.raises(RuntimeError, match="No embedding API key"):
        missing_key.embed(["offline"])

    empty_client = SimpleNamespace(
        embeddings=SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[])]
            )
        )
    )
    empty = OpenAICompatibleEmbedder(
        model="fake", api_key=None, base_url=None, client=empty_client
    )
    with pytest.raises(RuntimeError, match="empty vector"):
        empty.embed(["x"])

    settings = settings_for(isolated_workspace)
    hash_provider, hash_profile = provider_from_settings(settings)
    assert isinstance(hash_provider, HashEmbedder)
    assert hash_profile.dimension == 256
    api_provider, api_profile = provider_from_settings(
        replace(
            settings,
            embedding_provider="openai_compatible",
            embedding_model="fake",
        )
    )
    assert isinstance(api_provider, OpenAICompatibleEmbedder)
    assert api_profile.dimension is None
    with pytest.raises(ValueError, match="Unsupported"):
        provider_from_settings(replace(settings, embedding_provider="unknown"))
