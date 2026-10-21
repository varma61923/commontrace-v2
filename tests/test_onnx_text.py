"""Real tokenization and ONNX inference, using small local graphs without downloads."""
from __future__ import annotations

import json
import socket

import pytest

np = pytest.importorskip("numpy")
onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")
tokenizers = pytest.importorskip("tokenizers")
hub = pytest.importorskip("huggingface_hub")

from unimem import embeddings  # noqa: E402

MODEL = "BAAI/bge-small-en-v1.5"


@pytest.fixture
def graph_cache(tmp_path, monkeypatch):
    def create(*, pooled=False, dimension=384, nonfinite=False):
        tokenizer = tokenizers.Tokenizer(tokenizers.models.WordLevel(
            {"[PAD]": 0, "[UNK]": 1, "alpha": 2, "beta": 3, "gamma": 4}, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
        tokenizer.save(str(tmp_path / "tokenizer.json"))
        for name, data in (("config.json", {"pad_token_id": 0}),
                           ("tokenizer_config.json", {"model_max_length": 4096, "pad_token": "[PAD]"}),
                           ("special_tokens_map.json", {"pad_token": "[PAD]"})):
            (tmp_path / name).write_text(json.dumps(data))
        weights = np.arange(5 * dimension, dtype=np.float32).reshape(5, dimension) + 1
        if nonfinite:
            weights[2, 0] = np.nan
        inputs = [onnx.helper.make_tensor_value_info(name, onnx.TensorProto.INT64, [None, None])
                  for name in ("input_ids", "attention_mask", "token_type_ids")]
        nodes = [onnx.helper.make_node("Gather", ["weights", "input_ids"], ["tokens"])]
        initializers = [onnx.numpy_helper.from_array(weights, name="weights")]
        if pooled:
            initializers.append(onnx.numpy_helper.from_array(np.array(0, dtype=np.int64), name="first"))
            nodes.append(onnx.helper.make_node("Gather", ["tokens", "first"], ["vectors"], axis=1))
        else:
            nodes.append(onnx.helper.make_node("Identity", ["tokens"], ["vectors"]))
        output_shape = [None, dimension] if pooled else [None, None, dimension]
        output = onnx.helper.make_tensor_value_info("vectors", onnx.TensorProto.FLOAT, output_shape)
        graph = onnx.helper.make_graph(nodes, "local-text-fixture", inputs, [output], initializer=initializers)
        model = onnx.helper.make_model(graph, opset_imports=[onnx.helper.make_opsetid("", 13)], ir_version=8)
        onnx.checker.check_model(model)
        onnx.save(model, tmp_path / "model_optimized.onnx")
        downloads = []

        def cached(**kwargs):
            downloads.append(kwargs)
            return str(tmp_path)

        monkeypatch.setattr(hub, "snapshot_download", cached)
        return weights, downloads
    return create


@pytest.mark.parametrize("pooled", [False, True])
def test_real_cpu_inference_keeps_vectors_ordered_normalized_and_bounded(graph_cache, pooled):
    weights, downloads = graph_cache(pooled=pooled)
    provider = embeddings.provider("fastembed:" + MODEL, local_files_only=True)
    texts = ["alpha gamma", "beta"] * 33
    vectors = provider.embed(texts, query=False)
    assert len(vectors) == len(texts)
    expected = weights[[2, 3]] / np.linalg.norm(weights[[2, 3]], axis=1, keepdims=True)
    np.testing.assert_allclose(vectors, np.tile(expected, (33, 1)), atol=1e-7)
    np.testing.assert_allclose(provider.embed(["beta"], query=True), expected[1:2], atol=1e-7)
    assert provider.embed([], query=True) == []
    engine = provider._load()
    assert engine._session.get_providers() == ["CPUExecutionProvider"]
    options = engine._session.get_session_options()
    assert options.intra_op_num_threads == options.inter_op_num_threads == 2
    assert downloads == [{
        "repo_id": embeddings._ONNX_MODELS[MODEL][0], "revision": embeddings._ONNX_MODELS[MODEL][1],
        "cache_dir": downloads[0]["cache_dir"], "allow_patterns": list(embeddings._ONNX_MODEL_FILES),
        "local_files_only": True,
    }]


def test_tokenization_clips_to_512_and_sets_masks_and_segment_ids(graph_cache):
    graph_cache()
    engine = embeddings._OnnxText(model_name=MODEL, local_files_only=True)
    actual = engine._session
    calls = []

    class RecordingSession:
        def run(self, names, inputs):
            calls.append(inputs)
            return actual.run(names, inputs)

    engine._session = RecordingSession()
    list(engine.query_embed(["alpha " * 700, "beta"]))
    assert calls[0]["input_ids"].shape == (2, 512)
    assert calls[0]["attention_mask"].sum(axis=1).tolist() == [512, 1]
    assert not calls[0]["token_type_ids"].any()
    for kwargs in ({"batch_size": 65}, {"batch_size": 0}, {"parallel": 2}):
        with pytest.raises(ValueError, match="bounded"):
            list(engine.passage_embed(["alpha"], **kwargs))


@pytest.mark.parametrize("kwargs", [{"dimension": 385}, {"nonfinite": True}])
def test_corrupt_graph_output_is_rejected(graph_cache, kwargs):
    graph_cache(**kwargs)
    engine = embeddings._OnnxText(model_name=MODEL, local_files_only=True)
    with pytest.raises(ValueError, match="invalid CPU ONNX"):
        list(engine.query_embed(["alpha"]))


def test_a_partial_cached_snapshot_fails_closed(graph_cache, tmp_path):
    graph_cache()
    (tmp_path / "tokenizer.json").unlink()
    with pytest.raises(FileNotFoundError, match="tokenizer.json"):
        embeddings._OnnxText(model_name=MODEL, local_files_only=True)


@pytest.mark.parametrize("offline", [False, True])
def test_an_empty_cache_never_falls_back_to_network(tmp_path, monkeypatch, offline):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("cache-only inference must not access the network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("UNIMEM_OFFLINE", "1" if offline else "0")
    provider = embeddings.provider("fastembed:" + MODEL, local_files_only=not offline)
    with pytest.raises(embeddings.EmbeddingError, match="not available in the local cache"):
        provider.embed(["alpha"], query=True)
    assert not attempts
