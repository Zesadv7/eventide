"""Offline embedding determinism and adapter validation."""

import math
from types import SimpleNamespace

import pytest

from eventide.rag.embedding import (
    HashEmbedder,
    OpenAICompatibleEmbedder,
    vector_from_blob,
    vector_to_blob,
)


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

