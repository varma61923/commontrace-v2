"""The semantic arm's embedding model is part of the treatment: a fused label
names it (except the original model, whose labels are unchanged), the gate
threshold is set per model, and a store rebuilding its index from nothing
keeps the model its experiment ranked with."""
from __future__ import annotations

import json
import os

from commontrace import holdout_io, rerank_arm, retrieval_io

ARCTIC = "Snowflake/snowflake-arctic-embed-m-v1.5"
MPNET = "multi-qa-mpnet-base-dot-v1"


def test_labels_name_the_embedder_except_the_original():
    assert retrieval_io.eligibility_label("idf-v2", "gated", "cross-encoder-fast") == \
        "ce:tinybert2(gated(idf-v2+semantic))"
    assert retrieval_io.eligibility_label("idf-v2", "gated", "cross-encoder-fast",
                                          embedder="arctic-m") == \
        "ce:tinybert2(gated(idf-v2+semantic@arctic-m))"
    assert retrieval_io.eligibility_label("idf-v2", "none", embedder="arctic-m") == "idf-v2"
    assert retrieval_io.embedder_tag(MPNET) == ""
    assert retrieval_io.embedder_tag(ARCTIC) == "arctic-m"
    assert retrieval_io.embedder_tag(None) == ""


def test_labels_parse_back():
    label = "ce:minilm6(rrf(idf-v3+semantic@arctic-m))"
    assert retrieval_io.parse_eligibility_label(label) == ("idf-v3", "rrf")
    assert retrieval_io.parse_rerank_label(label)[1] == "cross-encoder"
    assert retrieval_io.parse_embedder(label) == "arctic-m"
    assert retrieval_io.parse_embedder("gated(idf-v2+semantic)") == ""
    assert retrieval_io.parse_embedder("idf-v2") == ""
    assert retrieval_io.semantic_only_label() == "semantic"
    assert retrieval_io.semantic_only_label("arctic-m") == "semantic@arctic-m"
    assert retrieval_io.parse_embedder("semantic@arctic-m") == "arctic-m"
    assert retrieval_io.parse_eligibility_label("semantic@arctic-m") == ("idf-v2", "none")


def test_config_label_carries_the_embedder_only_when_fused():
    config = retrieval_io.RetrievalConfig(fusion="gated", rerank="cross-encoder")
    assert config.eligibility_label_for(fused=True, embedder="arctic-m") == \
        "gated(idf-v2+semantic@arctic-m)"
    assert config.eligibility_label_for(fused=False, embedder="arctic-m") == "idf-v2"


def test_gate_threshold_is_per_embedder():
    assert rerank_arm.gate_threshold("cross-encoder-fast") == -4.0
    assert rerank_arm.gate_threshold("cross-encoder", "arctic-m") == -8.0
    assert rerank_arm.gate_threshold("cross-encoder-fast", "arctic-m") == -8.0
    # A model this build has no threshold for gets the strictest one.
    assert rerank_arm.gate_threshold("cross-encoder", "unknown") == -4.0
    admit = rerank_arm.admit_gated({"a"}, "cross-encoder-fast", "arctic-m")
    assert admit("a", -50.0) and admit("b", -7.5) and not admit("b", -8.5)
    legacy = rerank_arm.admit_gated(set(), "cross-encoder-fast")
    assert not legacy("b", -7.5)


def _log(root, scorer):
    path = holdout_io.holdout_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "o1", "lesson": "x", "arm": "treatment",
                             "scorer": scorer, "floor": 0.04}) + "\n")


def test_a_rebuilt_index_keeps_the_logged_model(tmp_path):
    root = str(tmp_path)
    assert retrieval_io.logged_embedding_model(root) is None
    _log(root, "ce:tinybert2(gated(idf-v2+semantic))")
    assert retrieval_io.logged_embedding_model(root) == MPNET
    _log(root, "ce:tinybert2(gated(idf-v2+semantic@arctic-m))")
    assert retrieval_io.logged_embedding_model(root) == ARCTIC
    _log(root, "semantic@arctic-m")  # the semantic arm alone pins its model too
    assert retrieval_io.logged_embedding_model(root) == ARCTIC
    _log(root, "semantic")
    assert retrieval_io.logged_embedding_model(root) == MPNET
    _log(root, "idf-v2")  # a lexical ranking pins no model
    assert retrieval_io.logged_embedding_model(root) is None


def test_the_cli_passes_the_logged_model_to_the_builder(tmp_path):
    from commontrace.commands.query_cmd import _fallback_model_args

    root = str(tmp_path)
    assert _fallback_model_args(root) == []
    _log(root, "rrf(idf-v2+semantic)")
    assert _fallback_model_args(root) == ["--fallback-model", MPNET]


def test_the_reference_scripts_trust_the_same_models():
    import importlib.util

    from commontrace.commands._shellout import packaged_reference_dir

    def load(name):
        spec = importlib.util.spec_from_file_location(
            f"ref_{name}", os.path.join(packaged_reference_dir(), f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    query, build = load("query"), load("build_index")
    assert set(query.TRUSTED_MODELS) == set(build.TRUSTED_MODELS) == set(retrieval_io.EMBEDDER_TAGS)
    assert query.DEFAULT_MODEL_NAME == build.DEFAULT_MODEL_NAME == ARCTIC


def test_pool_depth_is_per_embedder():
    assert rerank_arm.pool_size(5) == 30
    assert rerank_arm.pool_size(5, "cross-encoder") == 30  # lexical arm alone, or mpnet
    assert rerank_arm.pool_size(5, "cross-encoder", "arctic-m") == 10
    assert rerank_arm.pool_size(5, "cross-encoder-fast", "arctic-m") == 15
    assert rerank_arm.pool_size(40, "cross-encoder", "arctic-m") == 40  # never below the page


def test_new_stores_default_to_the_accurate_reranker(monkeypatch):
    monkeypatch.delenv(retrieval_io.DEFAULT_RERANK_ENV, raising=False)
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    assert retrieval_io.default_rerank() == retrieval_io.RERANK_CE
