"""No-network local-model preparation, identity, bounded inference and explicit fallback."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eventide.config import Settings
from eventide.rag import local_models
from eventide.rag.embedding import HashEmbedder, provider_from_settings
from eventide.rag.index import IndexStore
from eventide.rag.local_models import (
    LocalCrossEncoder,
    LocalModelError,
    LocalSemanticEmbedder,
    load_local_model,
    prepare_local_model,
    reranker_from_settings,
)
from eventide.rag.search import (
    format_results,
    hybrid_candidates_from_vector,
    hybrid_search,
    retrieval_from_vector,
    retrieve,
)
from eventide.rag.tool import handler
from tests.test_runtime import settings_for


def _source(tmp_path: Path, kind: str = "embedding") -> Path:
    source = tmp_path / f"source-{kind}"
    source.mkdir()
    (source / "config.json").write_text('{"model_type":"bert"}')
    (source / "tokenizer_config.json").write_text('{}')
    (source / "model.safetensors").write_bytes(b"fake weights; never loaded outside fake module")
    if kind == "embedding":
        (source / "modules.json").write_text(json.dumps([
            {"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"},
        ]))
    return source


@pytest.fixture
def prepared(tmp_path):
    return prepare_local_model(
        _source(tmp_path), tmp_path / "prepared", kind="embedding", revision="a" * 40,
    )


@pytest.fixture
def fake_library(monkeypatch):
    calls = []

    class SentenceTransformer:
        def __init__(self, path, **kwargs):
            calls.append(("embedding_init", path, kwargs))

        def encode(self, texts, **kwargs):
            calls.append(("encode", texts, kwargs))
            return [[1.0, 0.0] for _ in texts]

    class CrossEncoder:
        def __init__(self, path, **kwargs):
            calls.append(("reranker_init", path, kwargs))

        def predict(self, pairs, **kwargs):
            calls.append(("predict", pairs, kwargs))
            return list(range(len(pairs)))

    library = SimpleNamespace(SentenceTransformer=SentenceTransformer, CrossEncoder=CrossEncoder)
    monkeypatch.setattr(local_models, "version", lambda _name: local_models.LIBRARY_VERSION)
    monkeypatch.setattr(local_models.importlib, "import_module", lambda _name: library)
    return library, calls


def test_prepare_is_explicit_immutable_and_profile_binds_content_revision(prepared, tmp_path):
    same = load_local_model(prepared.path, kind="embedding")
    assert same == prepared
    first = LocalSemanticEmbedder(prepared.path).profile()
    other = prepare_local_model(
        prepared.path, tmp_path / "other", kind="embedding", revision="b" * 40,
    )
    second = LocalSemanticEmbedder(other.path).profile()
    assert first.model != second.model and first.dimension is None and first.base_url == "local"
    assert first.resolved(2).fingerprint() != second.resolved(2).fingerprint()
    with pytest.raises(LocalModelError, match="new_absolute_destination"):
        prepare_local_model(prepared.path, prepared.path, kind="embedding", revision="a" * 40)


@pytest.mark.parametrize("revision", ["main", "latest", "v1", "", "g" * 40])
def test_prepare_rejects_mutable_or_invalid_revisions(tmp_path, revision):
    with pytest.raises(LocalModelError, match="fixed_revision"):
        prepare_local_model(
            _source(tmp_path), tmp_path / "out", kind="embedding", revision=revision,
        )


@pytest.mark.parametrize("path", ["org/remote-model", "https://host/model", "./models"])
def test_remote_model_ids_and_relative_paths_never_reach_optional_library(path, monkeypatch):
    def forbidden():
        pytest.fail("model ID reached an ML library")
    monkeypatch.setattr(local_models, "_library", forbidden)
    with pytest.raises(LocalModelError, match="absolute_directory"):
        LocalSemanticEmbedder(Path(path))


@pytest.mark.parametrize("tamper", ["weights", "extra", "manifest", "missing"])
def test_prepared_model_drift_is_rejected_before_inference(prepared, fake_library, tamper):
    embedder = LocalSemanticEmbedder(prepared.path)
    if tamper == "weights":
        (prepared.path / "model.safetensors").write_bytes(b"changed")
    elif tamper == "extra":
        (prepared.path / "added.json").write_text('{}')
    elif tamper == "manifest":
        value = json.loads((prepared.path / local_models.MANIFEST).read_text())
        value["revision"] = "b" * 40
        (prepared.path / local_models.MANIFEST).write_text(json.dumps(value))
    else:
        (prepared.path / "model.safetensors").unlink()
    with pytest.raises(LocalModelError):
        embedder.embed(["query"])
    assert fake_library[1] == []


@pytest.mark.parametrize(
    "attack", ["leaf_link", "directory_link", "custom_module", "module_path",
               "remote_config", "pickle"],
)
def test_model_code_paths_links_and_pickle_weights_are_rejected(tmp_path, attack):
    source = _source(tmp_path)
    if attack == "leaf_link":
        outside = tmp_path / "outside.json"
        outside.write_text('{}')
        (source / "external.json").symlink_to(outside)
    elif attack == "directory_link":
        (source / "external").symlink_to(tmp_path, target_is_directory=True)
    elif attack in {"custom_module", "module_path"}:
        modules = json.loads((source / "modules.json").read_text())
        modules[0]["type" if attack == "custom_module" else "path"] = (
            "os.system" if attack == "custom_module" else "../outside"
        )
        (source / "modules.json").write_text(json.dumps(modules))
    elif attack == "remote_config":
        (source / "config.json").write_text('{"auto_map":{"AutoModel":"evil.module"}}')
    else:
        (source / "pytorch_model.bin").write_bytes(b"pickle")
    with pytest.raises(LocalModelError):
        prepare_local_model(source, tmp_path / "out", kind="embedding", revision="a" * 40)
    assert not (tmp_path / "out").exists()


def test_model_manifest_and_file_limits_are_enforced(tmp_path, monkeypatch):
    source = _source(tmp_path)
    monkeypatch.setattr(local_models, "MAX_MODEL_BYTES", 8)
    with pytest.raises(LocalModelError, match="too_large"):
        prepare_local_model(source, tmp_path / "out", kind="embedding", revision="a" * 40)
    assert not list(tmp_path.glob(".eventide-model-*"))


def test_embedding_dependency_is_lazy_and_missing_dependency_is_explicit(prepared, monkeypatch):
    calls = []
    def missing(_name):
        calls.append(_name)
        raise local_models.PackageNotFoundError(_name)
    monkeypatch.setattr(local_models, "version", missing)
    embedder = LocalSemanticEmbedder(prepared.path)
    assert calls == [] and embedder.embed([]) == []
    with pytest.raises(LocalModelError, match="dependency_missing"):
        embedder.embed(["query"])
    assert calls == ["sentence-transformers"]


def test_dependency_version_must_match_verified_adapter(prepared, monkeypatch):
    monkeypatch.setattr(local_models, "version", lambda _name: "999.0")
    with pytest.raises(LocalModelError, match="dependency_version"):
        LocalSemanticEmbedder(prepared.path).embed(["query"])


def test_embedding_batch_options_and_model_load_are_bounded(prepared, fake_library):
    embedder = LocalSemanticEmbedder(prepared.path)
    vectors = embedder.embed(["query"] * 35)
    assert len(vectors) == 35 and all(vector == [1.0, 0.0] for vector in vectors)
    calls = fake_library[1]
    init = calls[0]
    assert init[0] == "embedding_init" and init[2] == {
        "device": "cpu", "local_files_only": True, "trust_remote_code": False,
        "token": False, "model_kwargs": {"use_safetensors": True},
    }
    encoded = [call for call in calls if call[0] == "encode"]
    assert [len(call[1]) for call in encoded] == [16, 16, 3]
    assert all(call[2]["normalize_embeddings"] and call[2]["batch_size"] == 16 for call in encoded)
    embedder.embed(["again"])
    assert len([call for call in calls if call[0] == "embedding_init"]) == 1


@pytest.mark.parametrize("values", [[], [[float("nan")]], [[1.0], [2.0]], [[]]])
def test_invalid_embedding_outputs_are_not_indexed(prepared, fake_library, values):
    fake_library[0].SentenceTransformer.encode = lambda *_args, **_kwargs: values
    with pytest.raises(LocalModelError, match="invalid_response"):
        LocalSemanticEmbedder(prepared.path).embed(["one"])


def test_mutation_during_model_inference_is_rejected(prepared, fake_library):
    def changed(*_args, **_kwargs):
        (prepared.path / "config.json").write_text('{}')
        return [[1.0, 0.0]]
    fake_library[0].SentenceTransformer.encode = changed
    with pytest.raises(LocalModelError, match="local_model_changed"):
        LocalSemanticEmbedder(prepared.path).embed(["query"])


def test_crossencoder_lazy_load_cap_and_options(tmp_path, fake_library):
    model = prepare_local_model(
        _source(tmp_path, "reranker"), tmp_path / "ready", kind="reranker", revision="a" * 40,
    )
    reranker = LocalCrossEncoder(model.path)
    assert fake_library[1] == [] and reranker.score("query", []) == []
    assert reranker.score("query", ["document"] * 20) == list(range(20))
    init, predict = fake_library[1]
    assert init[2]["local_files_only"] and not init[2]["trust_remote_code"]
    assert init[2]["max_length"] == 512 and predict[2]["batch_size"] == 8
    assert predict[1] == [("query", "document")] * 20
    with pytest.raises(ValueError, match="At most 20"):
        reranker.score("query", ["document"] * 21)
    assert len(fake_library[1]) == 2


def test_settings_opt_in_paths_preserve_hash_default(prepared, monkeypatch, tmp_path):
    settings = settings_for(tmp_path)
    assert reranker_from_settings(settings) is None
    assert isinstance(provider_from_settings(settings)[0], HashEmbedder)
    opted = replace(
        settings, embedding_provider="local_semantic", embedding_model_path=prepared.path,
    )
    embedder, profile = provider_from_settings(opted)
    assert isinstance(embedder, LocalSemanticEmbedder) and profile.model == prepared.fingerprint
    with pytest.raises(LocalModelError):
        provider_from_settings(replace(settings, embedding_provider="local_semantic"))
    monkeypatch.setenv("EVENTIDE_EMBEDDING_MODEL_PATH", str(prepared.path))
    monkeypatch.setenv("EVENTIDE_RERANKER_MODEL_PATH", str(tmp_path / "not-yet-prepared"))
    env = Settings.from_env(tmp_path)
    assert env.embedding_model_path == prepared.path
    assert reranker_from_settings(env) is not None


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".git/info/exclude").write_text('.eventide/\n')
    for number in range(30):
        (root / f"file{number:02}.txt").write_text(f"common query document number {number}")
    index = IndexStore(root)
    index.build()
    return root, HashEmbedder(), index.embedding_profile()


def test_existing_hybrid_and_precomputed_vector_return_identical_baseline(corpus):
    root, embedder, profile = corpus
    old = hybrid_search(root, "common query", embedder=embedder, profile=profile)
    vector = embedder.embed(["common query"])[0]
    new = retrieval_from_vector(root, "common query", vector=vector, profile=profile)
    assert tuple(old) == new.results and new.mode == "dense+sparse" and not new.diagnostics
    assert all(item.rerank_score is None for item in new.results)


@pytest.mark.parametrize("k", [1, 5, 10])
def test_reranker_only_sees_20_candidates_and_keeps_rrf_scores(corpus, k):
    root, embedder, profile = corpus
    vector = embedder.embed(["common query"])[0]
    base = hybrid_candidates_from_vector(root, "common query", vector, profile=profile)
    seen = []
    class Reverse:
        def score(self, query, passages):
            seen.extend(passages)
            return [float(index) for index in range(len(passages))]
    outcome = retrieval_from_vector(
        root, "common query", vector=vector, profile=profile, reranker=Reverse(), k=k,
    )
    assert len(seen) == 20 and len(outcome.results) == min(k, 5)
    assert outcome.mode == "dense+sparse+rerank" and outcome.diagnostics == ()
    for position, item in enumerate(outcome.results, 1):
        original = base[-position]
        assert item.chunk == original.chunk and item.score == original.score
        assert item.rerank_score == 20 - position and item.rerank_rank == position
    rendered = format_results(list(outcome.results), mode=outcome.mode)
    assert "CrossEncoder score" in rendered and "RRF score unchanged" in rendered


@pytest.mark.parametrize("failure", ["missing", "exception", "nan", "count"])
def test_reranker_failure_retains_rrf_and_reports_reason(corpus, tmp_path, failure):
    root, embedder, profile = corpus
    class Failed:
        def score(self, query, passages):
            if failure == "exception":
                raise RuntimeError("sensitive internal message")
            return [float("nan")] * len(passages) if failure == "nan" else []
    reranker = LocalCrossEncoder(tmp_path / "missing") if failure == "missing" else Failed()
    outcome = retrieve(root, "common query", embedder=embedder, profile=profile, reranker=reranker)
    assert outcome.mode == "dense+sparse" and len(outcome.results) == 5
    assert len(outcome.diagnostics) == 1
    assert all(item.rerank_score is None for item in outcome.results)
    assert "sensitive" not in format_results(
        list(outcome.results), mode=outcome.mode, diagnostics=outcome.diagnostics,
    )


def test_local_embedding_failure_explicitly_falls_back_to_sparse_and_tool_reports_it(corpus):
    root, _embedder, profile = corpus
    class Missing:
        def embed(self, texts):
            raise LocalModelError("local_model_dependency_missing")
    outcome = retrieve(root, "common query", embedder=Missing(), profile=profile)
    assert outcome.mode == "sparse" and outcome.diagnostics == ("local_model_dependency_missing",)
    assert all(item.sources == ("sparse",) and item.dense_rank is None for item in outcome.results)
    text = handler(root, Missing(), profile)("common query")
    assert "mode: sparse" in text and "Retrieval degraded: local_model_dependency_missing" in text


def test_default_import_and_hash_path_do_not_import_ml_dependencies(tmp_path):
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'transformers', 'sentence_transformers'}:
        raise AssertionError('optional ML dependency imported')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from eventide.rag.search import hybrid_search
from eventide.rag.embedding import HashEmbedder
assert len(HashEmbedder().embed(['offline'])[0]) == 256
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)


@pytest.mark.skipif(
    not os.getenv("EVENTIDE_TEST_LOCAL_EMBEDDING") or not os.getenv("EVENTIDE_TEST_LOCAL_RERANKER"),
    reason="explicit prepared local models and optional retrieval dependencies are required",
)
def test_real_local_models_with_network_blocked(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("local inference attempted network access")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    embedding = LocalSemanticEmbedder(Path(os.environ["EVENTIDE_TEST_LOCAL_EMBEDDING"]))
    reranker = LocalCrossEncoder(Path(os.environ["EVENTIDE_TEST_LOCAL_RERANKER"]))
    texts = ["User passwords are updated in account settings.", "Clouds form in the atmosphere."]
    vectors = embedding.embed(texts)
    scores = reranker.score("How do I reset a password?", texts)
    assert len(vectors) == 2 and len(vectors[0]) == len(vectors[1]) > 0
    assert len(scores) == 2 and scores[0] != scores[1]
    root = tmp_path / "real"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".git/info/exclude").write_text('.eventide/\n')
    for index, text in enumerate(texts):
        (root / f"doc{index}.txt").write_text(text)
    IndexStore(root).build(embedder=embedding, profile=embedding.profile())
    result = retrieve(
        root, "How do I reset a password?", embedder=embedding,
        profile=embedding.profile(), reranker=reranker,
    )
    assert result.mode == "dense+sparse+rerank" and not result.diagnostics
    assert len(result.results) == 2
    assert all(item.rerank_score is not None for item in result.results)


    async def verify_host_path():
        from eventide.host import RuntimeHost
        from eventide.models import RunRequest
        from eventide.providers import ScriptedProvider
        from tests.test_runtime import settings_for

        provider = ScriptedProvider([
            {"tool_calls": [{"id": "local-search", "name": "search_knowledge",
                             "arguments": {"query": "How do I reset a password?"}}]},
            {"text": "done"},
        ])
        settings = replace(
            settings_for(root), state_dir=tmp_path / "host-state",
            embedding_provider="local_semantic", embedding_model_path=embedding.prepared.path,
            reranker_model_path=reranker.path,
        )
        host = RuntimeHost(settings, provider, enable_mcp=False)
        try:
            result = await host.run(RunRequest("search local knowledge", mode="plan"))
            assert result.status == "completed"
            completed = next(event for event in host.store.run_events(result.run_id)
                             if event["type"] == "tool.completed")
            assert not completed["payload"]["is_error"]
            assert "dense+sparse+rerank" in completed["payload"]["content"]
            assert "CrossEncoder score" in completed["payload"]["content"]
        finally:
            await host.close()

    asyncio.run(verify_host_path())


@pytest.mark.parametrize("sparse_only", [True, False])
def test_production_outcome_caps_five_without_reranker(corpus, sparse_only):
    root, embedder, profile = corpus
    vector = None if sparse_only else embedder.embed(["common query"])[0]
    outcome = retrieval_from_vector(
        root, "common query", vector=vector, profile=None if sparse_only else profile, k=10,
    )
    assert len(outcome.results) == 5
    assert outcome.mode == ("sparse" if sparse_only else "dense+sparse")
