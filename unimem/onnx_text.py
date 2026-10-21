"""Pinned CPU text models for the existing ``fastembed:`` provider tags.

Only BGE text inference is needed here. Loading the ONNX graph directly avoids
pulling image decoders into text retrieval. Imports remain optional and lazy.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

# The same quantized artifacts used by FastEmbed's BGE v1.5 text providers.
# Pin snapshots so cache-only servers never need a mutable branch lookup.
MODELS = {
    "BAAI/bge-small-en-v1.5": (
        "qdrant/bge-small-en-v1.5-onnx-q", "aa8f8b060edb00e03bfdd08813a2949946c8ba55", 384,
    ),
    "BAAI/bge-base-en-v1.5": (
        "qdrant/bge-base-en-v1.5-onnx-q", "199291fdd1aa89faf9c20b722dc72ad5e17aa0d0", 768,
    ),
}
MODEL_FILES = ("model_optimized.onnx", "config.json", "tokenizer.json",
               "tokenizer_config.json", "special_tokens_map.json")


class TextEmbedding:
    """BGE v1.5 CLS embeddings with bounded CPU inference and cache-only loading."""

    def __init__(self, *, model_name: str, cache_dir: str | None = None,
                 threads: int = 2, local_files_only: bool = False):
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import AddedToken, Tokenizer

        if model_name not in MODELS:
            raise ValueError(f"unsupported CPU ONNX text model: {model_name}")
        if type(threads) is not int or not 1 <= threads <= 2:
            raise ValueError("CPU ONNX text inference requires one or two threads")
        repository, revision, self.embedding_size = MODELS[model_name]
        self.artifact_directory = Path(snapshot_download(
            repo_id=repository, revision=revision,
            cache_dir=cache_dir or str(Path(tempfile.gettempdir()) / "fastembed_cache"),
            allow_patterns=list(MODEL_FILES), local_files_only=local_files_only,
        ))
        for name in MODEL_FILES:
            if not (self.artifact_directory / name).is_file():
                raise FileNotFoundError(f"cached text model is missing {name}")

        config = self._json("config.json")
        tokenizer_config = self._json("tokenizer_config.json")
        self._tokenizer = Tokenizer.from_file(str(self.artifact_directory / "tokenizer.json"))
        limits = [512]
        for key in ("model_max_length", "max_length"):
            limit = tokenizer_config.get(key)
            if type(limit) is int and limit > 0:
                limits.append(limit)
        self._tokenizer.enable_truncation(max_length=min(limits))
        if not self._tokenizer.padding:
            self._tokenizer.enable_padding(pad_id=config.get("pad_token_id", 0),
                                           pad_token=tokenizer_config.get("pad_token", "[PAD]"))
        for token in self._json("special_tokens_map.json").values():
            self._tokenizer.add_special_tokens([AddedToken(**token) if isinstance(token, dict) else token])

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = threads
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(self.artifact_directory / "model_optimized.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"],
        )
        self._inputs = {node.name for node in self._session.get_inputs()}
        if "input_ids" not in self._inputs or self._inputs - {"input_ids", "attention_mask", "token_type_ids"}:
            raise ValueError("unsupported CPU ONNX text model inputs")

    def _json(self, filename: str) -> dict:
        with (self.artifact_directory / filename).open(encoding="utf-8") as handle:
            return json.load(handle)

    def passage_embed(self, texts: list[str], *, batch_size: int = 64, parallel=None):
        import numpy as np

        if type(batch_size) is not int or not 1 <= batch_size <= 64 or parallel is not None:
            raise ValueError("CPU ONNX text batches must be bounded to 64 with no worker processes")
        for start in range(0, len(texts), batch_size):
            encoded = self._tokenizer.encode_batch(texts[start:start + batch_size])
            ids = np.asarray([row.ids for row in encoded], dtype=np.int64)
            inputs = {"input_ids": ids}
            if "attention_mask" in self._inputs:
                inputs["attention_mask"] = np.asarray([row.attention_mask for row in encoded], dtype=np.int64)
            if "token_type_ids" in self._inputs:
                inputs["token_type_ids"] = np.zeros_like(ids)
            output = self._session.run(None, inputs)[0]
            # These BGE v1.5 models use CLS pooling for both queries and passages.
            vectors = output[:, 0] if output.ndim == 3 else output
            if vectors.shape != (len(encoded), self.embedding_size) or not np.isfinite(vectors).all():
                raise ValueError("invalid CPU ONNX text embedding output")
            norms = np.maximum(np.linalg.norm(vectors, ord=2, axis=1, keepdims=True), 1e-12)
            yield from vectors / norms

    def query_embed(self, texts: list[str], *, batch_size: int = 64, parallel=None):
        yield from self.passage_embed(texts, batch_size=batch_size, parallel=parallel)
