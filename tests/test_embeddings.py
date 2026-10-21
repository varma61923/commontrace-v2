"""Multi-provider embeddings: tag parsing, request shapes, normalization, batching and the vector cache."""
from __future__ import annotations

import math

import pytest

from unimem import embeddings


class _Recorder:
    def __init__(self, reply):
        self.calls, self.reply = [], reply

    def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        return self.reply(url, payload)


def _unit(v):
    return math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-6)


@pytest.mark.parametrize("tag, provider, model, dims", [
    ("arctic-m", "local", "arctic-m", None),
    ("nomic", "local", "nomic", None),
    ("local:BAAI/bge-base-en-v1.5", "local", "BAAI/bge-base-en-v1.5", None),
    ("fastembed:baai/bge-small-en-v1.5", "fastembed", "BAAI/bge-small-en-v1.5", None),
    ("fastembed:BAAI/bge-base-en-v1.5", "fastembed", "BAAI/bge-base-en-v1.5", None),
    ("openai:text-embedding-3-large@1024", "openai", "text-embedding-3-large", 1024),
    ("gemini:gemini-embedding-001", "gemini", "gemini-embedding-001", None),
    ("voyage:voyage-3.5@512", "voyage", "voyage-3.5", 512),
    ("cohere:embed-v4.0", "cohere", "embed-v4.0", None),
    ("ollama:nomic-embed-text", "ollama", "nomic-embed-text", None),
    ("compat:bge-m3", "compat", "bge-m3", None),
])
def test_tags_parse_to_a_provider_model_and_dimensions(tag, provider, model, dims):
    spec = embeddings.parse(tag)
    assert (spec.provider, spec.model, spec.dimensions) == (provider, model, dims)
    assert embeddings.parse(spec.tag) == spec
    assert "/" not in spec.cache_name and ":" not in spec.cache_name


@pytest.mark.parametrize("bad", ["", "nope", "acme:model", "arctic-m@64", "openai:m@8", "openai:a b", "../x"])
def test_bad_tags_are_refused(bad):
    with pytest.raises(embeddings.EmbeddingError):
        embeddings.parse(bad)


def test_fastembed_rejects_unregistered_models_and_dimensions():
    with pytest.raises(embeddings.EmbeddingError, match="unsupported FastEmbed model"):
        embeddings.parse("fastembed:BAAI/bge-large-en-v1.5")
    with pytest.raises(embeddings.EmbeddingError, match="come from its model registry"):
        embeddings.parse("fastembed:BAAI/bge-small-en-v1.5@256")


def test_openai_request_shape_dimensions_order_and_normalization(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    post = _Recorder(lambda url, p: {"data": [{"index": i, "embedding": [3.0, 4.0] + [0.0] * 254}
                                             for i in reversed(range(len(p["input"])))]})
    vectors = embeddings.provider("openai:text-embedding-3-small@256", post=post).embed(["a", "b"], query=True)
    (url, headers, payload), = post.calls
    assert url == "https://api.openai.com/v1/embeddings" and headers["Authorization"] == "Bearer sk-test"
    assert payload == {"model": "text-embedding-3-small", "input": ["a", "b"], "encoding_format": "float",
                       "dimensions": 256}
    assert [v[:2] for v in vectors] == [[0.6, 0.8], [0.6, 0.8]] and all(_unit(v) for v in vectors)


def test_requested_dimensions_are_enforced(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    post = _Recorder(lambda url, p: {"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    with pytest.raises(embeddings.EmbeddingError, match="requested 256"):
        embeddings.provider("openai:text-embedding-3-small@256", post=post).embed(["a"], query=False)


def test_gemini_uses_retrieval_task_types_and_the_key_header(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    post = _Recorder(lambda url, p: {"embeddings": [{"values": [1.0] * 768} for _ in p["requests"]]})
    p = embeddings.provider("gemini:gemini-embedding-001@768", post=post)
    p.embed(["doc"], query=False)
    p.embed(["question"], query=True)
    (url, headers, doc), (_u, _h, query) = post.calls
    assert url.endswith("/models/gemini-embedding-001:batchEmbedContents") and headers["x-goog-api-key"] == "g-test"
    assert doc["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT" and query["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert doc["requests"][0]["outputDimensionality"] == 768


def test_voyage_cohere_and_ollama_shapes(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("COHERE_API_KEY", "c")
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    voyage = _Recorder(lambda url, p: {"data": [{"index": 0, "embedding": [1, 2]}]})
    embeddings.provider("voyage:voyage-3.5", post=voyage).embed(["x"], query=True)
    assert voyage.calls[0][2]["input_type"] == "query"
    cohere = _Recorder(lambda url, p: {"embeddings": {"float": [[1, 2]]}})
    embeddings.provider("cohere:embed-v4.0", post=cohere).embed(["x"], query=False)
    assert cohere.calls[0][0] == "https://api.cohere.com/v2/embed"
    assert cohere.calls[0][2]["input_type"] == "search_document"
    ollama = _Recorder(lambda url, p: {"embeddings": [[1, 2]]})
    embeddings.provider("ollama:nomic-embed-text", post=ollama).embed(["x"], query=False)
    assert ollama.calls[0][0] == "http://127.0.0.1:11434/api/embed"


def test_large_inputs_are_batched_and_clipped(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "c")
    post = _Recorder(lambda url, p: {"embeddings": {"float": [[1.0, 0.0]] * len(p["texts"])}})
    out = embeddings.provider("cohere:embed-v4.0", post=post).embed(["y" * 50_000] * 200, query=False)
    assert len(out) == 200 and [len(c[2]["texts"]) for c in post.calls] == [96, 96, 8]
    assert max(len(t) for c in post.calls for t in c[2]["texts"]) == embeddings.MAX_CHARS


def test_short_replies_bad_vectors_and_missing_keys_fail_clearly(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "c")
    with pytest.raises(embeddings.EmbeddingError, match="returned 1 vectors for 2"):
        embeddings.provider("cohere:embed-v4.0", post=_Recorder(
            lambda url, p: {"embeddings": {"float": [[1.0]]}})).embed(["a", "b"], query=False)
    with pytest.raises(embeddings.EmbeddingError, match="zero vector"):
        embeddings.provider("cohere:embed-v4.0", post=_Recorder(
            lambda url, p: {"embeddings": {"float": [[0.0, 0.0]]}})).embed(["a"], query=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY_FILE", raising=False)
    with pytest.raises(embeddings.EmbeddingError, match="VOYAGE_API_KEY"):
        embeddings.provider("voyage:voyage-3.5", post=_Recorder(lambda u, p: {})).embed(["a"], query=False)


def test_offline_mode_refuses_hosted_providers(monkeypatch):
    from unimem import llm

    monkeypatch.setenv("UNIMEM_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(llm.LLMUnavailable, match="offline"):
        embeddings.provider("openai:text-embedding-3-small").embed(["a"], query=False)


def test_fastembed_uses_bounded_cpu_batches_and_query_document_apis(monkeypatch, tmp_path):
    calls = []

    class FakeTextEmbedding:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def query_embed(self, texts, *, batch_size, parallel):
            calls.append(("query", list(texts), batch_size, parallel))
            return iter([[3.0, 4.0] for _ in texts])

        def passage_embed(self, texts, *, batch_size, parallel):
            calls.append(("passage", list(texts), batch_size, parallel))
            return iter([[3.0, 4.0] for _ in texts])

    monkeypatch.setattr("unimem.onnx_text.TextEmbedding", FakeTextEmbedding)
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))
    provider = embeddings.provider("fastembed:BAAI/bge-small-en-v1.5", local_files_only=True)

    query_vectors = provider.embed(["question"] * 65, query=True)
    document_vectors = provider.embed(["fact"], query=False)

    assert len(query_vectors) == 65 and all(_unit(row) for row in query_vectors)
    assert document_vectors == [[0.6, 0.8]]
    assert calls[0] == ("init", {
        "model_name": "BAAI/bge-small-en-v1.5", "cache_dir": str(tmp_path), "threads": 2,
        "local_files_only": True,
    })
    assert [(call[0], len(call[1]), call[2], call[3]) for call in calls[1:]] == [
        ("query", 64, 64, None), ("query", 1, 64, None), ("passage", 1, 64, None),
    ]


def test_fastembed_cache_miss_is_cache_only_and_does_not_touch_network(monkeypatch):
    modes = []

    class CacheOnlyMiss:
        def __init__(self, *, local_files_only, **kwargs):
            modes.append(local_files_only)
            if not local_files_only:
                raise AssertionError("a cache miss must not retry through the download path")
            raise FileNotFoundError("model missing from cache")

    monkeypatch.setattr("unimem.onnx_text.TextEmbedding", CacheOnlyMiss)
    monkeypatch.setenv("UNIMEM_OFFLINE", "1")
    provider = embeddings.provider("fastembed:BAAI/bge-small-en-v1.5")

    with pytest.raises(embeddings.EmbeddingError, match="not available in the local cache"):
        provider.embed(["question"], query=True)
    assert modes == [True]


def test_missing_native_runtime_names_the_installable_extra(monkeypatch):
    def missing(**_kwargs):
        raise ImportError("onnxruntime is not installed")

    monkeypatch.setattr("unimem.onnx_text.TextEmbedding", missing)
    provider = embeddings.provider("fastembed:BAAI/bge-small-en-v1.5", local_files_only=True)
    with pytest.raises(embeddings.EmbeddingError, match=r"pip install 'unimem\[fastembed\]'"):
        provider.embed(["question"], query=True)


def test_fastembed_projection_identity_binds_model_revision_version_and_dimension(
        monkeypatch, tmp_path):
    model_dir = tmp_path / "cached-model"
    model_dir.mkdir()
    (model_dir / "model.onnx").write_bytes(b"fake onnx artifact")
    dimensions = {"BAAI/bge-small-en-v1.5": 384, "BAAI/bge-base-en-v1.5": 768}

    class FakeTextEmbedding:
        def __init__(self, *, model_name, **kwargs):
            dimension = dimensions[model_name]
            self.embedding_size = dimension
            self.artifact_directory = model_dir

            def query_embed(texts, **_kwargs):
                return iter([[1.0] + [0.0] * (dimension - 1) for _ in texts])

            self.query_embed = query_embed
            self.passage_embed = query_embed

    monkeypatch.setattr("unimem.onnx_text.TextEmbedding", FakeTextEmbedding)
    monkeypatch.setattr("importlib.metadata.version", lambda package: "2.0.0")

    small = embeddings.provider("fastembed:BAAI/bge-small-en-v1.5")
    base = embeddings.provider("fastembed:BAAI/bge-base-en-v1.5")
    small_identity, small_dimension = small.projection_identity()
    base_identity, base_dimension = base.projection_identity()

    assert small_dimension == 384 and base_dimension == 768
    assert small_identity != base_identity
    assert "BAAI/bge-small-en-v1.5" in small_identity
    assert "BAAI/bge-base-en-v1.5" in base_identity
    assert "unimem-onnx-text-v1-" in small_identity and "v2.0.0-sha256-" in small_identity


def test_fastembed_conversation_availability_needs_all_cpu_dependencies(monkeypatch):
    from unimem.conversation import embed

    dependencies = ("numpy", "onnxruntime", "tokenizers", "huggingface_hub")
    found = dict.fromkeys(dependencies, object())
    monkeypatch.setattr(embed.importlib.util, "find_spec", lambda name: found.get(name))
    assert embed.available_for("fastembed:BAAI/bge-small-en-v1.5")
    for dependency in dependencies:
        found[dependency] = None
        assert not embed.available_for("fastembed:BAAI/bge-small-en-v1.5")
        found[dependency] = object()


def test_custom_providers_register_once():
    class Fixed:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            return [[1.0, 0.0] for _ in texts]

    embeddings.register("acmetest", Fixed)
    assert embeddings.provider("acmetest:m1").embed(["a"], query=True) == [[1.0, 0.0]]
    with pytest.raises(embeddings.EmbeddingError):
        embeddings.register("acmetest", Fixed)
    with pytest.raises(embeddings.EmbeddingError):
        embeddings.register("openai", Fixed)


def test_conversation_vectors_from_a_hosted_provider_are_cached_by_content(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from unimem.conversation import embed

    calls = []

    class Counting:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            calls.append((tuple(texts), query))
            return [[float(len(t)), 1.0, 0.5] for t in texts]

    local_files_modes = []

    def build_provider(tag, post=None, *, local_files_only=False):
        local_files_modes.append(local_files_only)
        return Counting(embeddings.parse(tag))

    monkeypatch.setattr(embeddings, "provider", build_provider)
    encoder = embed.Embedder(str(tmp_path), "openai:text-embedding-3-small")
    first = encoder.vectors([("h1", "alpha"), ("h2", "beta")])
    again = encoder.vectors([("h1", "alpha"), ("h2", "beta"), ("h1", "alpha")])
    assert first.shape == (2, 3) and again.shape == (3, 3) and calls == [(("alpha", "beta"), False)]
    encoder.encode(["question"], query=True)
    encoder.encode(["question"], query=True)
    assert calls[-1] == (("question",), True) and len(calls) == 2
    assert local_files_modes == [False]
    encoder.close()
    assert embed.model_identity("openai:text-embedding-3-small") == "openai:text-embedding-3-small"
    assert embed.model_identity("arctic-m") == embed.MODELS["arctic-m"][0]
    assert (tmp_path / "memory" / "conversations" / "embeddings-openai_text-embedding-3-small.db").exists()
