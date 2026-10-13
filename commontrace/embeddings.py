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
  (``COMMONTRACE_EMBED_BASE_URL``, optional ``COMMONTRACE_EMBED_API_KEY``).

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

import math
import os
import re
from dataclasses import dataclass
from typing import Protocol

from commontrace.configuration import getenv as _configuration_getenv

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
_TAG = re.compile(r"^(?:(?P<provider>[a-z]+):)?(?P<model>[A-Za-z0-9._/\-]{1,128})(?:@(?P<dims>[0-9]{1,5}))?$")


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
    from commontrace.secrets_provider import env_secret

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
            from commontrace.conversation import embed

            if not embed.available():
                raise EmbeddingError("local embeddings need the attention extra: pip install 'commontrace[attention]'")
            from sentence_transformers import SentenceTransformer

            from commontrace.rerank_arm import _no_progress_bars

            with _no_progress_bars():
                from commontrace import offline

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


class _FastEmbed:
    """Optional bounded CPU provider; server callers must use its local cache."""

    def __init__(self, spec: Spec, *, local_files_only: bool = False):
        self.spec = spec
        self.local_files_only = local_files_only
        self._model = None
        cache = _configuration_getenv("FASTEMBED_CACHE_PATH", "").strip() or None
        if not cache and os.path.isdir("/tmp/commontrace-fastembed-cache"):
            cache = "/tmp/commontrace-fastembed-cache"
        self._cache_dir = cache

    def _load(self):
        if self._model is None:
            from commontrace import offline

            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise EmbeddingError(
                    "FastEmbed embeddings need the optional dependency: "
                    "pip install 'commontrace[fastembed]'"
                ) from exc
            cache_only = self.local_files_only or offline.enabled()
            try:
                # FastEmbed 0.8.0 forwards local_files_only to its model manager,
                # which prevents both Hub and GCS downloads on the server path.
                self._model = TextEmbedding(
                    model_name=self.spec.model,
                    cache_dir=self._cache_dir,
                    threads=2,
                    local_files_only=cache_only,
                )
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
        out: list[list[float]] = []
        # Keep inference batches and ONNX thread use bounded in the caller.
        for start in range(0, len(texts), BATCH["fastembed"]):
            batch = _clip(texts[start:start + BATCH["fastembed"]])
            rows = list(method(batch, batch_size=BATCH["fastembed"], parallel=None))
            if len(rows) != len(batch):
                raise EmbeddingError(
                    f"fastembed returned {len(rows)} vectors for {len(batch)} texts"
                )
            out.extend(_normalize(row) for row in rows)
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
        model_dir = getattr(getattr(model, "model", None), "_model_dir", None)
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
        probe = self.embed(["CommonTrace local projection identity probe."], query=True)
        if len(probe) != 1 or len(probe[0]) != dimension:
            raise EmbeddingError("FastEmbed model output does not match its registered dimension")
        return f"fastembed-{self.spec.model}-v{version('fastembed')}-{revision}", dimension


class _Hosted:
    """One class, one request shape per provider; every path returns rows in input order."""

    def __init__(self, spec: Spec, post=None):
        self.spec = spec
        from commontrace import llm

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
            return self._openai_like(base + "/embeddings", _secret("OPENAI_API_KEY", "COMMONTRACE_OPENAI_API_KEY"),
                                     texts)
        if p == "compat":
            base = _configuration_getenv("COMMONTRACE_EMBED_BASE_URL", "").rstrip("/")
            if not base:
                raise EmbeddingError("set COMMONTRACE_EMBED_BASE_URL to an OpenAI-compatible /v1 endpoint")
            from commontrace.secrets_provider import env_secret

            return self._openai_like(base + "/embeddings", env_secret("COMMONTRACE_EMBED_API_KEY") or None, texts)
        if p == "gemini":
            key = _secret("GEMINI_API_KEY", "GOOGLE_API_KEY", "COMMONTRACE_GEMINI_API_KEY")
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
    if not re.fullmatch(r"[a-z][a-z0-9]{1,31}", name) or name in (*LOCAL_PROVIDERS, *HOSTED) or name in _CUSTOM:
        raise EmbeddingError("provider names are lowercase, new and not built in")
    _CUSTOM[name] = factory


def provider(tag: str | Spec, *, post=None, local_files_only: bool = False) -> Provider:
    """The provider for a tag. Building it is cheap; local models load on first use."""
    if isinstance(tag, Spec) and tag.provider in _CUSTOM:
        return _CUSTOM[tag.provider](tag)  # type: ignore[operator]
    if isinstance(tag, str) and ":" in tag and tag.split(":", 1)[0] in _CUSTOM:
        name, model = tag.split(":", 1)
        return _CUSTOM[name](Spec(name, model))  # type: ignore[operator]
    spec = tag if isinstance(tag, Spec) else parse(tag)
    if spec.provider in _CUSTOM:
        return _CUSTOM[spec.provider](spec)  # type: ignore[operator]
    if spec.provider == "local":
        return _Local(spec, local_files_only=local_files_only)
    if spec.provider == "fastembed":
        return _FastEmbed(spec, local_files_only=local_files_only)
    return _Hosted(spec, post=post)


def known() -> dict[str, list[str]]:
    """What can be selected, for help text and the capability matrix."""
    return {
        "local": sorted(LOCAL_MODELS),
        "local_providers": ["fastembed:<supported-model>"],
        "hosted": [f"{p}:{DEFAULT_MODELS.get(p, '<model>')}" for p in HOSTED],
        "custom": sorted(_CUSTOM),
    }
