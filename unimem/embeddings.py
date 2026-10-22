"""Semantic embeddings from one interface over local and hosted providers.

A provider is chosen by a tag:

- ``arctic-m``, ``minilm``, ``bge-small``, ``e5-small``, ``nomic``: local
  sentence-transformers models (the attention extra), run on CPU;
- ``local:<huggingface-model>``: any other local sentence-transformers model;
- ``fastembed:BAAI/bge-small-en-v1.5``: optional CPU ONNX model (the
  ``fastembed`` extra); the shipped default only reads cached weights;
- ``openai:<model>``: OpenAI embeddings (``OPENAI_API_KEY``);
- ``gemini:<model>``: Google Gemini embeddings (``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``);
- ``voyage:<model>``: Voyage AI (``VOYAGE_API_KEY``);
- ``cohere:<model>``: Cohere (``COHERE_API_KEY``);
- ``ollama:<model>``: a local Ollama server (``OLLAMA_HOST``, default 127.0.0.1:11434);
- ``compat:<model>``: any OpenAI-compatible endpoint, e.g. vLLM, TEI or LM Studio
  (``UNIMEM_EMBED_BASE_URL``, optional ``UNIMEM_EMBED_API_KEY``).

A ``@N`` suffix requests N dimensions where the provider supports truncation
(Matryoshka models: OpenAI text-embedding-3, Gemini, Voyage, Cohere v4).
Every key may be given as ``NAME_FILE`` instead. Vectors are always returned
unit-normalized, so cosine similarity is a dot product, and query and document
inputs are embedded with each provider's retrieval-specific mode.

Hosted calls go through ``llm._post_json``: HTTPS (or loopback HTTP), no
redirects, bounded responses, retries on 429 and 5xx, and refused entirely in
offline mode. Texts are truncated to ``MAX_CHARS`` before they leave the host.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from unimem.configuration import getenv as _configuration_getenv

MAX_CHARS = 24_000
_RETRIEVAL = "Represent this sentence for searching relevant passages: "
LOCAL_MODELS = {
    "arctic-m": ("Snowflake/snowflake-arctic-embed-m-v1.5", _RETRIEVAL, ""),
    "minilm": ("sentence-transformers/all-MiniLM-L6-v2", "", ""),
    "bge-small": ("BAAI/bge-small-en-v1.5", _RETRIEVAL, ""),
    "e5-small": ("intfloat/e5-small-v2", "query: ", "passage: "),
    "nomic": ("nomic-ai/nomic-embed-text-v1.5", "search_query: ", "search_document: "),
}
HOSTED = ("openai", "gemini", "voyage", "cohere", "ollama", "compat")
LOCAL_PROVIDERS = ("local", "fastembed")
DEFAULT_LOCAL_TAG = "fastembed:BAAI/bge-small-en-v1.5"
FASTEMBED_MODELS = {
    "baai/bge-small-en-v1.5": ("BAAI/bge-small-en-v1.5", 384),
    "baai/bge-base-en-v1.5": ("BAAI/bge-base-en-v1.5", 768),
}
DEFAULT_MODELS = {"openai": "text-embedding-3-small", "gemini": "gemini-embedding-001", "voyage": "voyage-3.5",
                  "cohere": "embed-v4.0", "ollama": "nomic-embed-text"}
BATCH = {
    "openai": 128,
    "gemini": 100,
    "voyage": 128,
    "cohere": 96,
    "ollama": 64,
    "compat": 64,
    "local": 64,
    "fastembed": 64,
}
_CUSTOM: dict[str, object] = {}
_CUSTOM_LOCK = threading.RLock()
_TAG = re.compile(r"^(?:(?P<provider>[a-z][a-z0-9]{1,31}):)?(?P<model>[A-Za-z0-9._/\-]{1,128})"
                  r"(?:@(?P<dims>[0-9]{1,5}))?$")


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Spec:
    provider: str  # one of LOCAL_PROVIDERS or HOSTED
    model: str
    dimensions: int | None = None

    @property
    def tag(self) -> str:
        """A canonical, filesystem-safe identity: what the vector cache is keyed by."""
        local = self.provider == "local" and self.model in LOCAL_MODELS
        base = self.model if local else f"{self.provider}:{self.model}"
        return base + (f"@{self.dimensions}" if self.dimensions else "")

    @property
    def cache_name(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", self.tag)

    @property
    def hosted(self) -> bool:
        return self.provider in HOSTED


def parse(tag: str) -> Spec:
    """Parse an embedder tag; raises EmbeddingError with the accepted forms."""
    match = _TAG.match((tag or "").strip())
    if not match:
        raise EmbeddingError(f"embedder {tag!r} is not a tag: use a local name ({', '.join(sorted(LOCAL_MODELS))}), "
                             "local:<model>, fastembed:<supported-model>, or "
                             "<provider>:<model>[@dims] with provider in " + ", ".join(HOSTED))
    provider, model, dims = match.group("provider"), match.group("model"), match.group("dims")
    dimensions = int(dims) if dims else None
    if dimensions is not None and not 32 <= dimensions <= 8192:
        raise EmbeddingError("requested dimensions must be between 32 and 8192")
    if provider is None:
        if model not in LOCAL_MODELS:
            raise EmbeddingError(f"unknown local embedder {model!r}; known: {', '.join(sorted(LOCAL_MODELS))}")
        if dimensions:
            raise EmbeddingError("local models do not truncate; drop the @dims suffix")
        return Spec("local", model)
    if provider == "local":
        return Spec("local", model)
    if provider == "fastembed":
        if dimensions:
            raise EmbeddingError("fastembed model dimensions come from its model registry")
        registered = FASTEMBED_MODELS.get(model.lower())
        if registered is None:
            raise EmbeddingError(
                f"unsupported FastEmbed model {model!r}; known: "
                + ", ".join(model for model, _ in FASTEMBED_MODELS.values())
            )
        return Spec("fastembed", registered[0])
    if provider in _CUSTOM:
        return Spec(provider, model, dimensions)
    if provider not in HOSTED:
        raise EmbeddingError(
            f"unknown embedding provider {provider!r}; known: local, fastembed, {', '.join(HOSTED)}"
        )
    return Spec(provider, model, dimensions)


class Provider(Protocol):
    spec: Spec

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]: ...


def _normalize(vector) -> list[float]:
    values = [float(x) for x in vector]
    if not values or not all(math.isfinite(x) for x in values):
        raise EmbeddingError("the provider returned an empty or non-finite vector")
    norm = math.sqrt(sum(x * x for x in values))
    if norm == 0.0:
        raise EmbeddingError("the provider returned a zero vector")
    return [x / norm for x in values]


def _secret(*names: str) -> str:
    from unimem.secrets_provider import env_secret

    for name in names:
        value = env_secret(name)
        if value:
            return value
    raise EmbeddingError(f"set {names[0]} (or {names[0]}_FILE) to use this embedding provider")


def _clip(texts: list[str]) -> list[str]:
    return [(t or " ")[:MAX_CHARS] for t in texts]


class _Local:
    def __init__(self, spec: Spec, *, local_files_only: bool = False):
        self.spec = spec
        self.local_files_only = local_files_only
        name, self._query_prefix, self._doc_prefix = LOCAL_MODELS.get(spec.model, (spec.model, "", ""))
        self._name = name
        self._model = None

    def _load(self):
        if self._model is None:
            from unimem.conversation import embed

            if not embed.available():
                raise EmbeddingError("local embeddings need the attention extra: pip install 'unimem[attention]'")
            from sentence_transformers import SentenceTransformer

            from unimem.rerank_arm import _no_progress_bars

            with _no_progress_bars():
                from unimem import offline

                cache_only = self.local_files_only or offline.enabled()
                try:
                    model = SentenceTransformer(
                        self._name,
                        device="cpu",
                        local_files_only=True,
                        trust_remote_code=self.spec.model == "nomic",
                    )
                except Exception as exc:  # noqa: BLE001 - backend-specific cache misses vary
                    if offline.enabled():
                        raise offline.OfflineError(
                            f"offline mode: local embedding model {self._name!r} is not available in the cache"
                        ) from exc
                    if cache_only:
                        raise EmbeddingError(
                            f"local embedding model {self._name!r} is not available in the cache"
                        ) from exc
                    model = SentenceTransformer(
                        self._name, device="cpu", trust_remote_code=self.spec.model == "nomic"
                    )
            model.max_seq_length = 256
            self._model = model
        return self._model

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        prefix = self._query_prefix if query else self._doc_prefix
        vectors = self._load().encode([prefix + t for t in _clip(texts)], batch_size=BATCH["local"],
                                      normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        return [_normalize(v) for v in vectors]


# The same quantized artifacts used by FastEmbed's BGE v1.5 text providers.
# Pin snapshots so cache-only servers never need a mutable branch lookup.
_ONNX_MODELS = {
    "BAAI/bge-small-en-v1.5": (
        "qdrant/bge-small-en-v1.5-onnx-q", "aa8f8b060edb00e03bfdd08813a2949946c8ba55", 384,
    ),
    "BAAI/bge-base-en-v1.5": (
        "qdrant/bge-base-en-v1.5-onnx-q", "199291fdd1aa89faf9c20b722dc72ad5e17aa0d0", 768,
    ),
}
_ONNX_MODEL_FILES = ("model_optimized.onnx", "config.json", "tokenizer.json",
               "tokenizer_config.json", "special_tokens_map.json")


ONNX_DEFAULT_THREADS = 2
ONNX_MAX_THREADS = 64


def onnx_threads() -> int:
    """CPU threads for local ONNX text inference.

    Serving keeps two per model so concurrent tenants cannot starve each other.
    Bulk ingestion hosts may raise it with UNIMEM_EMBED_THREADS (at most the CPUs present).
    """
    raw = _configuration_getenv("UNIMEM_EMBED_THREADS", "").strip()
    if not raw:
        return ONNX_DEFAULT_THREADS
    if not raw.isdigit() or not 1 <= int(raw) <= ONNX_MAX_THREADS:
        raise EmbeddingError(f"UNIMEM_EMBED_THREADS must be an integer from 1 to {ONNX_MAX_THREADS}")
    return min(int(raw), max(1, os.cpu_count() or 1))


class _OnnxText:
    """BGE v1.5 CLS embeddings with bounded CPU inference and cache-only loading."""

    def __init__(self, *, model_name: str, cache_dir: str | None = None,
                 threads: int = ONNX_DEFAULT_THREADS, local_files_only: bool = False):
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import AddedToken, Tokenizer

        if model_name not in _ONNX_MODELS:
            raise ValueError(f"unsupported CPU ONNX text model: {model_name}")
        if type(threads) is not int or not 1 <= threads <= ONNX_MAX_THREADS:
            raise ValueError(f"CPU ONNX text inference requires 1 to {ONNX_MAX_THREADS} threads")
        repository, revision, self.embedding_size = _ONNX_MODELS[model_name]
        self.artifact_directory = Path(snapshot_download(
            repo_id=repository, revision=revision,
            cache_dir=cache_dir or str(Path(tempfile.gettempdir()) / "fastembed_cache"),
            allow_patterns=list(_ONNX_MODEL_FILES), local_files_only=local_files_only,
        ))
        for name in _ONNX_MODEL_FILES:
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


class _FastEmbed:
    """Optional bounded CPU provider; server callers must use its local cache."""

    def __init__(self, spec: Spec, *, local_files_only: bool = False):
        self.spec = spec
        self.local_files_only = local_files_only
        self._model = None
        cache = _configuration_getenv("FASTEMBED_CACHE_PATH", "").strip() or None
        fallback_cache = os.path.join(tempfile.gettempdir(), "unimem-fastembed-cache")
        if not cache and os.path.isdir(fallback_cache):
            cache = fallback_cache
        self._cache_dir = cache

    def _load(self):
        if self._model is None:
            from unimem import offline

            cache_only = self.local_files_only or offline.enabled()
            try:
                # The text adapter resolves only pinned ONNX snapshots and
                # never retries a cache-only load through a download path.
                self._model = _OnnxText(
                    model_name=self.spec.model,
                    cache_dir=self._cache_dir,
                    threads=onnx_threads(),
                    local_files_only=cache_only,
                )
            except ImportError as exc:
                raise EmbeddingError(
                    "FastEmbed embeddings need the optional dependency: "
                    "pip install 'unimem[fastembed]'"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - optional backend errors vary
                if cache_only:
                    raise EmbeddingError(
                        f"FastEmbed model {self.spec.model!r} is not available in the local cache"
                    ) from exc
                raise EmbeddingError(f"FastEmbed could not load model {self.spec.model!r}") from exc
        return self._model

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        method = model.query_embed if query else model.passage_embed
        clipped = _clip(texts)
        # Batch texts of similar length together: each batch pads to its longest
        # text, so mixing a 500-token turn with short ones multiplied the work.
        order = sorted(range(len(clipped)), key=lambda i: len(clipped[i]))
        out: list[list[float] | None] = [None] * len(clipped)
        # Keep inference batches and ONNX thread use bounded in the caller.
        for start in range(0, len(order), BATCH["fastembed"]):
            indices = order[start:start + BATCH["fastembed"]]
            batch = [clipped[i] for i in indices]
            rows = list(method(batch, batch_size=BATCH["fastembed"], parallel=None))
            if len(rows) != len(batch):
                raise EmbeddingError(
                    f"fastembed returned {len(rows)} vectors for {len(batch)} texts"
                )
            for i, row in zip(indices, rows):
                out[i] = _normalize(row)
        dimensions = {len(row) for row in out}
        if len(dimensions) > 1:
            raise EmbeddingError("fastembed returned vectors of different dimensions")
        return out

    def projection_identity(self) -> tuple[str, int]:
        """Identify the exact HF snapshot or hash the cached model artifacts."""
        import re
        from hashlib import sha256
        from importlib.metadata import version
        from pathlib import Path

        model = self._load()
        model_dir = getattr(model, "artifact_directory", None)
        if not isinstance(model_dir, (str, Path)):
            raise EmbeddingError("FastEmbed model has no verifiable artifact directory")
        path = Path(model_dir)
        if not path.is_dir():
            raise EmbeddingError("FastEmbed model artifact directory is unavailable")

        parts = path.parts
        revision = next(
            (
                parts[index + 1]
                for index, part in enumerate(parts[:-1])
                if part == "snapshots"
                and re.fullmatch(r"[0-9a-f]{40,64}", parts[index + 1])
            ),
            None,
        )
        if revision is None:
            digest = sha256()
            files = sorted(
                file for file in path.rglob("*")
                if file.is_file() and file.name != "files_metadata.json"
            )
            if not files:
                raise EmbeddingError("FastEmbed model cache contains no artifacts")
            for file in files:
                relative = file.relative_to(path).as_posix().encode("utf-8")
                digest.update(len(relative).to_bytes(4, "big"))
                digest.update(relative)
                with file.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            revision = "sha256-" + digest.hexdigest()

        expected_dimension = FASTEMBED_MODELS.get(self.spec.model.lower(), (None, None))[1]
        dimension = getattr(model, "embedding_size", None)
        if type(dimension) is not int or dimension != expected_dimension:
            raise EmbeddingError("FastEmbed model dimension does not match the registered model")
        probe = self.embed(["UniMem local projection identity probe."], query=True)
        if len(probe) != 1 or len(probe[0]) != dimension:
            raise EmbeddingError("FastEmbed model output does not match its registered dimension")
        return f"unimem-onnx-text-v1-{self.spec.model}-v{version('unimem')}-{revision}", dimension


class _Hosted:
    """One class, one request shape per provider; every path returns rows in input order."""

    def __init__(self, spec: Spec, post=None):
        self.spec = spec
        from unimem import llm

        self._post = post or llm._post_json

    def _openai_like(self, url: str, key: str | None, texts: list[str]) -> list[list[float]]:
        payload: dict = {"model": self.spec.model, "input": texts, "encoding_format": "float"}
        if self.spec.dimensions:
            payload["dimensions"] = self.spec.dimensions
        headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + key} if key else {})}
        data = self._post(url, headers, payload)
        rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
        return [r.get("embedding") for r in rows]

    def _call(self, texts: list[str], query: bool) -> list:
        p, model = self.spec.provider, self.spec.model
        if p == "openai":
            base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            return self._openai_like(base + "/embeddings", _secret("OPENAI_API_KEY", "UNIMEM_OPENAI_API_KEY"),
                                     texts)
        if p == "compat":
            base = _configuration_getenv("UNIMEM_EMBED_BASE_URL", "").rstrip("/")
            if not base:
                raise EmbeddingError("set UNIMEM_EMBED_BASE_URL to an OpenAI-compatible /v1 endpoint")
            from unimem.secrets_provider import env_secret

            return self._openai_like(base + "/embeddings", env_secret("UNIMEM_EMBED_API_KEY") or None, texts)
        if p == "gemini":
            key = _secret("GEMINI_API_KEY", "GOOGLE_API_KEY", "UNIMEM_GEMINI_API_KEY")
            name = model if model.startswith("models/") else "models/" + model
            task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
            requests = []
            for t in texts:
                item = {"model": name, "content": {"parts": [{"text": t}]}, "taskType": task}
                if self.spec.dimensions:
                    item["outputDimensionality"] = self.spec.dimensions
                requests.append(item)
            url = f"https://generativelanguage.googleapis.com/v1beta/{name}:batchEmbedContents"
            data = self._post(url, {"Content-Type": "application/json", "x-goog-api-key": key}, {"requests": requests})
            return [e.get("values") for e in data.get("embeddings") or []]
        if p == "voyage":
            payload = {"model": model, "input": texts, "input_type": "query" if query else "document"}
            if self.spec.dimensions:
                payload["output_dimension"] = self.spec.dimensions
            data = self._post("https://api.voyageai.com/v1/embeddings",
                              {"Content-Type": "application/json",
                               "Authorization": "Bearer " + _secret("VOYAGE_API_KEY")}, payload)
            rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
            return [r.get("embedding") for r in rows]
        if p == "cohere":
            payload = {"model": model, "texts": texts, "embedding_types": ["float"],
                       "input_type": "search_query" if query else "search_document"}
            if self.spec.dimensions:
                payload["output_dimension"] = self.spec.dimensions
            data = self._post("https://api.cohere.com/v2/embed",
                              {"Content-Type": "application/json",
                               "Authorization": "Bearer " + _secret("COHERE_API_KEY")}, payload)
            return list((data.get("embeddings") or {}).get("float") or [])
        if p == "ollama":
            host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
            if "://" not in host:
                host = "http://" + host
            data = self._post(host + "/api/embed", {"Content-Type": "application/json"},
                              {"model": model, "input": texts, "truncate": True})
            return list(data.get("embeddings") or [])
        raise EmbeddingError(f"unknown provider {p!r}")

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        out: list[list[float]] = []
        texts = _clip(texts)
        size = BATCH[self.spec.provider]
        for start in range(0, len(texts), size):
            batch = texts[start:start + size]
            rows = self._call(batch, query)
            if len(rows) != len(batch):
                raise EmbeddingError(f"{self.spec.provider} returned {len(rows)} vectors for {len(batch)} texts")
            out.extend(_normalize(r or []) for r in rows)
        dims = {len(v) for v in out}
        if len(dims) > 1:
            raise EmbeddingError("the provider returned vectors of different dimensions")
        if self.spec.dimensions and dims and dims != {self.spec.dimensions}:
            raise EmbeddingError(f"requested {self.spec.dimensions} dimensions, received {dims.pop()}")
        return out


def register(name: str, factory) -> None:
    """Register trusted code: ``factory(spec) -> Provider`` for tags ``<name>:<model>``."""
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9]{1,31}", name)
            or name in (*LOCAL_PROVIDERS, *HOSTED) or not callable(factory)):
        raise EmbeddingError("provider names are lowercase, new and not built in")
    with _CUSTOM_LOCK:
        if name in _CUSTOM:
            raise EmbeddingError("provider already registered")
        _CUSTOM[name] = factory


def provider(tag: str | Spec, *, post=None, local_files_only: bool = False) -> Provider:
    """The provider for a tag. Building it is cheap; local models load on first use."""
    spec = tag if isinstance(tag, Spec) else parse(tag)
    with _CUSTOM_LOCK:
        custom = _CUSTOM.get(spec.provider)
    if custom is not None:
        return custom(spec)  # type: ignore[operator]
    if spec.provider == "local":
        return _Local(spec, local_files_only=local_files_only)
    if spec.provider == "fastembed":
        return _FastEmbed(spec, local_files_only=local_files_only)
    return _Hosted(spec, post=post)


def known() -> dict[str, list[str]]:
    """What can be selected, for help text and the capability matrix."""
    with _CUSTOM_LOCK:
        custom = sorted(_CUSTOM)
    return {
        "local": sorted(LOCAL_MODELS),
        "local_providers": ["fastembed:<supported-model>"],
        "hosted": [f"{p}:{DEFAULT_MODELS.get(p, '<model>')}" for p in HOSTED],
        "custom": custom,
    }
