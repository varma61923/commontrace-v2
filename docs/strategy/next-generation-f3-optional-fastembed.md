# F3: Optional CPU FastEmbed provider

FastEmbed is a viable optional dense provider behind `commontrace.embeddings`.
It uses ONNX Runtime instead of PyTorch and supports the same query/document
embedding contract. CommonTrace accepts only the two checked FastEmbed 0.8.0
registry entries that fit this bounded path: `BAAI/bge-small-en-v1.5` (384
dimensions) and `BAAI/bge-base-en-v1.5` (768 dimensions). The small model is the
recommended local candidate; FastEmbed lists it as MIT licensed and 0.067 GB.

The implementation follows the existing provider interface and keeps the
dependency opt-in as `commontrace[fastembed]`. It pins FastEmbed 0.8.0 and
Hugging Face Hub 0.36.2; ONNX Runtime is pinned to 1.23.2 for Python below 3.14
and 1.30.0 for Python 3.14 and newer. Cognee's checked-in lock has those same
FastEmbed, Hub, and runtime releases. No FastEmbed or ONNX package is part of
the base install or Hub image.

The Hub constructs this provider with `local_files_only=True`; offline mode
also forces cache-only construction in ordinary embedding calls. FastEmbed
0.8.0 forwards this setting through Hugging Face snapshot lookup and fails on
a cache miss rather than falling through to a network fetch. A conversation
embedding tag is considered available only when both NumPy and FastEmbed are
installed. Projection identity binds the canonical model ID, FastEmbed version,
the Hugging Face snapshot commit (or a SHA-256 of cached artifacts), adapter
version, and the registered dimension; the runtime checks an actual output
against that dimension before the Hub accepts the projection.

The competitor evidence is specific: Cognee at `0ec7a9fa6` includes
`fastembed<=0.8.0` in `pyproject.toml`, resolves keyless embeddings to
`BAAI/bge-small-en-v1.5` in
`cognee/infrastructure/databases/vector/embeddings/config.py`, and calls
FastEmbed's `TextEmbedding` API in
`cognee/infrastructure/databases/vector/embeddings/FastembedEmbeddingEngine.py`.
Its lock records FastEmbed 0.8.0, ONNX Runtime 1.23.2 for Python through 3.13,
ONNX Runtime 1.30.0 for Python 3.14, and Hugging Face Hub 0.36.2. Mem0 at
`b7ad69af` also declares FastEmbed as an optional embedding dependency and
locks 0.8.0. Hindsight at `a9eeed77f` instead keeps ONNX Runtime,
Transformers, Tokenizers, and Hugging Face Hub in its optional `local-onnx`
extra and loads an ONNX session with a Transformers tokenizer. CommonTrace
adapts FastEmbed's public provider API; it does not copy competitor code.

License evidence: the FastEmbed 0.8.0 project metadata says Apache License;
its registered BGE small and BGE base model entries say MIT. See the
[FastEmbed 0.8.0 project metadata](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/pyproject.toml),
[model registry](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/text/onnx_embedding.py),
[cache-only constructor contract](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/text/text_embedding_base.py),
and [model cache lookup](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/common/model_management.py).

Tests use fake provider modules and artifact directories. They cover tag and
dimension validation, bounded batching, query/document routing, normalized
vectors, model-name/revision identity, offline cache-miss behavior, Hub
cache-only construction, and conversation capability detection. They do not
install FastEmbed, download weights, measure retrieval quality, or measure
inference latency.

The Hub image remains incomplete for this path: it does not install the
optional runtime or provision a persistent model cache. The projection remains
unavailable until deployment explicitly installs the extra and mounts or
preloads the model cache. The ONNX wheel and model contribute material image
or cache size; their final deployed footprint is unmeasured here. This is an
optional CPU candidate, not a validated quality default.
