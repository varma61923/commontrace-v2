"""Optional executable local vendor profiles; never substitutes CommonTrace recall.

These raw-source profiles deliberately exclude managed APIs and LLM extraction.
Gold evidence and answers never enter them. Canonical Store is only a source map.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import importlib.metadata
import os
import shutil
import tempfile
import threading
from importlib.util import find_spec

from benchmarks.artifacts import model_artifact, package_source
from commontrace.conversation.search import Recall, tokens
from commontrace.conversation.timeparse import parse_moment

PROFILES = ("commontrace", "mem0-raw", "graphiti-episodic", "hindsight-raw")

# Hindsight's embedded client resolves its profile and pg0 directories through
# Path.home(). Keep its temporary HOME in place for the entire adapter lifetime
# so startup, API requests, and teardown all address the same isolated instance.
_HINDSIGHT_HOME_LOCK = threading.RLock()
_HINDSIGHT_HOME_KEYS = ("HOME", "USERPROFILE")


def pack(store, ranked_ids, budget, *, profile):
    """Whole source turns only, with attribution included in the budget."""
    ranked = list(dict.fromkeys(ranked_ids))
    turns = store.turns(ranked)
    if any(i not in turns for i in ranked):
        raise ValueError("vendor returned a source outside the bound corpus")
    chunks, kept = [], []
    for identity in ranked:
        turn = turns[identity]
        date = turn.at.isoformat() if turn.at else "undated"
        chunk = f"[{turn.ref} | {date} | {turn.speaker}]\n{turn.text}"
        if tokens("\n\n".join([*chunks, chunk])) <= budget:
            chunks.append(chunk)
            kept.append(identity)
    context = "\n\n".join(chunks)
    return Recall("", context, tokens(context), kept, ranked, None,
                  {"vendor_profile": profile, "rerank": "native", "packing": "whole-source-turns"})


def eligible_turns(store, now):
    moment = parse_moment(now)
    if now is not None and moment is None:
        raise ValueError("vendor profile requires a parseable history cutoff")
    ids = [row[0] for row in store.db.execute("SELECT id FROM turns ORDER BY id")]
    return [turn for turn in store.turns(ids).values()
            if moment is None or turn.at is None or turn.at <= moment]


def huggingface_cache_paths():
    """Return the already-resolved local model cache paths before HOME changes."""
    from huggingface_hub.constants import HF_HOME, HF_HUB_CACHE

    return str(HF_HOME), str(HF_HUB_CACHE)


class Mem0Raw:
    def __init__(self, directory, store, now):
        # Prevent benchmark text being sent to vendor usage telemetry.
        os.environ["MEM0_TELEMETRY"] = "False"
        from mem0 import Memory
        from mem0.memory import telemetry
        telemetry.MEM0_TELEMETRY = False

        self.store = store
        turns = eligible_turns(store, now)
        version = importlib.metadata.version("mem0ai")
        source_binding = package_source("mem0")
        embedding_binding = model_artifact("sentence-transformers/all-MiniLM-L6-v2")
        self.closed = False
        self.memory = Memory.from_config({
            "vector_store": {"provider": "qdrant", "config": {
                "collection_name": "benchmark", "embedding_model_dims": 384,
                "path": os.path.join(directory, "qdrant")}},
            "embedder": {"provider": "huggingface", "config": {
                "model": "sentence-transformers/all-MiniLM-L6-v2",
                "model_kwargs": {"local_files_only": True}}},
            # infer=False never dispatches to this deliberately unusable endpoint.
            "llm": {"provider": "openai", "config": {"model": "unused",
                "api_key": "local-profile-unused", "openai_base_url": "http://127.0.0.1:1/v1"}},
            "history_db_path": os.path.join(directory, "history.db"),
        })
        self.refs = {}
        texts = [f"{turn.speaker}: {turn.text}" for turn in turns]
        embed = self.memory.embedding_model.embed
        try:
            # Use the public embed_batch API, then preserve the public add path.
            # Only identical add inputs can use these vectors; restore on exit.
            vectors = self.memory.embedding_model.embed_batch(texts, "add")
            if len(vectors) != len(texts):
                raise ValueError("vendor batch embedding count mismatch")
            memo = dict(zip(texts, vectors))
            self.memory.embedding_model.embed = lambda text, memory_action=None: (
                memo[text] if memory_action == "add" and text in memo else embed(text, memory_action))
            for turn in turns:
                result = self.memory.add(
                    [{"role": "user", "content": f"{turn.speaker}: {turn.text}"}],
                    user_id=store.space, infer=False, metadata={"source_ref": turn.ref})
                added = result["results"]
                if len(added) != 1 or added[0]["event"] != "ADD":
                    raise ValueError("raw vendor ingestion did not preserve one source record")
                self.refs[added[0]["id"]] = turn.id
        except BaseException:
            self.close()
            raise
        finally:
            self.memory.embedding_model.embed = embed
        try:
            encoder = self.memory.vector_store._get_bm25_encoder()
            self.descriptor = {"profile": "mem0-raw", "version": version, "upstream_sha256": source_binding, "embedding_artifact": embedding_binding,
                "inference": False, "ingestion_embeddings": "public-batch-identical-input-memo", "embedder": "sentence-transformers/all-MiniLM-L6-v2",
                "sparse_encoder": "Qdrant/bm25" if encoder else None,
                "backend": "qdrant-local", "top_k": 200, "threshold": 0.0,
                "time_filter": "history-cutoff-before-indexing", "packing": "whole-source-turns"}

        except BaseException:
            self.close()
            raise

    def retrieve(self, question, budget):
        result = self.memory.search(question, filters={"user_id": self.store.space},
                                    top_k=200, threshold=0.0, rerank=False)
        identities = [self.refs[item["id"]] for item in result["results"]]
        return pack(self.store, identities, budget, profile="mem0-raw")

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.memory.close()
        self.memory.vector_store.client.close()
        client = getattr(self.memory.llm, "client", None)
        if client is not None:
            client.close()


class GraphitiEpisodic:
    def __init__(self, directory, store, now):
        os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.nodes import EpisodeType, EpisodicNode
        from redislite.async_falkordb_client import AsyncFalkorDB

        self.store, self.refs = store, {}
        # Falkor fulltext tokenizes punctuation even in escaped group filters.
        # Bind one punctuation-free namespace to the canonical case identity.
        self.group_id = hashlib.sha256(store.space.encode("utf-8")).hexdigest()
        turns = eligible_turns(store, now)
        version = importlib.metadata.version("graphiti-core")
        source_binding = package_source("graphiti_core")
        self.loop = asyncio.new_event_loop()
        self.client, self.driver = None, None
        self.closed = False
        try:
            self.client = AsyncFalkorDB(dbfilename=os.path.join(directory, "graph.rdb"))
            self.driver = FalkorDriver(falkor_db=self.client, database="benchmark")
            self.loop.run_until_complete(self.driver.build_indices_and_constraints())
            for turn in turns:
                node = EpisodicNode(name=str(turn.ref), group_id=self.group_id,
                    source=EpisodeType.message, source_description="raw conversation turn",
                    content=f"{turn.speaker}: {turn.text}",
                    valid_at=(turn.at or dt.datetime(1970, 1, 1)).replace(tzinfo=dt.timezone.utc))
                self.loop.run_until_complete(node.save(self.driver))
                self.refs[node.uuid] = turn.id
            self.descriptor = {"profile": "graphiti-episodic",
                "version": version, "upstream_sha256": source_binding, "inference": False,
                "search": "native-episode-bm25", "reranker": "rrf", "top_k": 200,
                "backend": "falkordblite", "time_filter": "history-cutoff-before-indexing",
                "packing": "whole-source-turns", "entity_graph": False,
                "group_namespace": "sha256-canonical-space-utf8"}
        except BaseException:
            self.close()
            raise

    def retrieve(self, question, budget):
        from graphiti_core.search.search import episode_search
        from graphiti_core.search.search_config import EpisodeSearchConfig, EpisodeSearchMethod
        from graphiti_core.search.search_filters import SearchFilters

        episodes, _scores = self.loop.run_until_complete(episode_search(
            self.driver, None, question, [], [self.group_id],
            EpisodeSearchConfig(search_methods=[EpisodeSearchMethod.bm25]), SearchFilters(), limit=200))
        identities = [self.refs[episode.uuid] for episode in episodes]
        return pack(self.store, identities, budget, profile="graphiti-episodic")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.client is not None:
                self.loop.run_until_complete(self.client.connection.shutdown(nosave=True))
        finally:
            try:
                if self.driver is not None:
                    self.loop.run_until_complete(self.driver.close())
                elif self.client is not None:
                    self.loop.run_until_complete(self.client.aclose())
            finally:
                self.loop.close()


class HindsightRaw:
    """Hindsight's public chunks-only path over local source turns.

    Hindsight documents that ``llm_provider=none`` forces chunks mode. In that
    mode each source chunk is stored verbatim as a world fact, with no LLM
    extraction. A local embedding model powers its native recall path; the
    availability check and descriptor binding below prevent an online model
    fetch from being hidden inside a benchmark run.
    """

    embedder = "BAAI/bge-small-en-v1.5"

    def __init__(self, directory, store, now):
        self.store = store
        self.refs = {}
        self.closed = False
        self._home_locked = False
        self._saved_env = {}
        _HINDSIGHT_HOME_LOCK.acquire()
        self._home_locked = True
        try:
            self._saved_env = {key: os.environ.get(key) for key in _HINDSIGHT_HOME_KEYS}
            self._saved_env.update({
                "HINDSIGHT_EMBED_API_DATABASE_URL": os.environ.get("HINDSIGHT_EMBED_API_DATABASE_URL"),
                "HINDSIGHT_API_DATABASE_URL": os.environ.get("HINDSIGHT_API_DATABASE_URL"),
                "HINDSIGHT_API_DATABASE_BACKEND": os.environ.get("HINDSIGHT_API_DATABASE_BACKEND"),
                "HINDSIGHT_API_READ_DATABASE_URL": os.environ.get("HINDSIGHT_API_READ_DATABASE_URL"),
                "HINDSIGHT_API_MIGRATION_DATABASE_URL": os.environ.get("HINDSIGHT_API_MIGRATION_DATABASE_URL"),
                "HINDSIGHT_API_LLM_PROVIDER": os.environ.get("HINDSIGHT_API_LLM_PROVIDER"),
                "HINDSIGHT_API_RETAIN_LLM_PROVIDER": os.environ.get("HINDSIGHT_API_RETAIN_LLM_PROVIDER"),
                "HINDSIGHT_API_EMBEDDINGS_PROVIDER": os.environ.get("HINDSIGHT_API_EMBEDDINGS_PROVIDER"),
                "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL": os.environ.get("HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL"),
                "HINDSIGHT_API_ENABLE_RERANKING": os.environ.get("HINDSIGHT_API_ENABLE_RERANKING"),
                "HINDSIGHT_API_OTEL_TRACES_ENABLED": os.environ.get("HINDSIGHT_API_OTEL_TRACES_ENABLED"),
                "HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT"),
                "HINDSIGHT_API_OTEL_EXPORTER_OTLP_HEADERS": os.environ.get("HINDSIGHT_API_OTEL_EXPORTER_OTLP_HEADERS"),
                "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"),
                "OTEL_EXPORTER_OTLP_HEADERS": os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"),
                "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
                "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE"),
                "HF_HOME": os.environ.get("HF_HOME"),
                "HF_HUB_CACHE": os.environ.get("HF_HUB_CACHE"),
                "TRANSFORMERS_CACHE": os.environ.get("TRANSFORMERS_CACHE"),
            })
            # Retain provider chains and explicit database overrides can turn a
            # nominally local profile into a remote call. Snapshot and remove
            # every such host override before selecting the isolated local path.
            for key, value in list(os.environ.items()):
                if key.startswith((
                    "HINDSIGHT_API_LLM_",
                    "HINDSIGHT_API_RETAIN_LLM_",
                    "HINDSIGHT_API_REFLECT_LLM_",
                    "HINDSIGHT_API_CONSOLIDATION_LLM_",
                    "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_",
                )):
                    self._saved_env.setdefault(key, value)
                    os.environ.pop(key, None)
            for key in (
                "HINDSIGHT_EMBED_API_DATABASE_URL",
                "HINDSIGHT_API_DATABASE_URL",
                "HINDSIGHT_API_DATABASE_BACKEND",
                "HINDSIGHT_API_READ_DATABASE_URL",
                "HINDSIGHT_API_MIGRATION_DATABASE_URL",
            ):
                os.environ.pop(key, None)
            if shutil.which("hindsight-api") is None:
                raise RuntimeError("hindsight-raw requires a locally installed hindsight-api executable; network fallback is disabled")
            if find_spec("sentence_transformers") is None:
                raise RuntimeError("hindsight-raw requires Hindsight's local-ml dependencies; network fallback is disabled")
            # The default cache is read-only for this path. If it is absent,
            # fail before Hindsight can attempt a download from Hugging Face.
            embedding_binding = model_artifact(self.embedder)
            hf_home, hf_hub_cache = huggingface_cache_paths()
            os.environ["HOME"] = os.path.join(directory, "home")
            os.makedirs(os.environ["HOME"], exist_ok=True)
            os.environ["USERPROFILE"] = os.environ["HOME"]
            os.environ["HINDSIGHT_API_LLM_PROVIDER"] = "none"
            os.environ["HINDSIGHT_API_RETAIN_LLM_PROVIDER"] = "none"
            os.environ["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] = "local"
            os.environ["HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL"] = self.embedder
            os.environ["HINDSIGHT_API_ENABLE_RERANKING"] = "false"
            os.environ["HINDSIGHT_API_OTEL_TRACES_ENABLED"] = "false"
            for key in (
                "HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT",
                "HINDSIGHT_API_OTEL_EXPORTER_OTLP_HEADERS",
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "OTEL_EXPORTER_OTLP_HEADERS",
            ):
                os.environ.pop(key, None)
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["HF_HOME"] = hf_home
            os.environ["HF_HUB_CACHE"] = hf_hub_cache
            os.environ["TRANSFORMERS_CACHE"] = hf_hub_cache

            from hindsight import HindsightEmbedded

            versions = {
                package: importlib.metadata.version(distribution)
                for package, distribution in (
                    ("hindsight_api", "hindsight-api-slim"),
                    ("hindsight_client", "hindsight-client"),
                    ("hindsight_embed", "hindsight-embed"),
                )
            }
            source_bindings = {
                package: package_source(package)
                for package in ("hindsight_api", "hindsight_client", "hindsight_embed", "hindsight")
            }
            self.client = HindsightEmbedded(
                profile="commontrace-benchmark",
                llm_provider="none",
                log_level="error",
            )
            self.bank_id = "commontrace-benchmark"
            cutoff_turns = eligible_turns(store, now)
            for turn in cutoff_turns:
                document_id = f"source-turn-{turn.id}"
                timestamp = turn.at.isoformat() if turn.at else "unset"
                result = self.client.retain(
                    bank_id=self.bank_id,
                    content=f"{turn.speaker}: {turn.text}",
                    timestamp=timestamp,
                    document_id=document_id,
                )
                if not getattr(result, "success", False) or getattr(result, "items_count", 0) != 1:
                    raise ValueError("Hindsight raw retain did not preserve one source turn")
                self.refs[document_id] = turn.id
            moment = parse_moment(now)
            self.query_timestamp = moment.isoformat() if moment else None
            self.descriptor = {
                "profile": "hindsight-raw",
                "versions": versions,
                "source_bindings": source_bindings,
                "inference": False,
                "ingestion": "public-retain-llm-provider-none-chunks",
                "embedder": self.embedder,
                "embedding_artifact": embedding_binding,
                "backend": "embedded-postgresql-pg0",
                "search": "native-four-arm-rrf",
                "reranker": "disabled",
                "telemetry": "disabled",
                "budget": "high",
                "top_k": 200,
                "time_filter": "history-cutoff-before-indexing",
                "packing": "whole-source-turns",
            }
        except BaseException:
            self.close()
            raise

    def retrieve(self, question, budget):
        result = self.client.recall(
            bank_id=self.bank_id,
            query=question,
            types=["world"],
            max_tokens=1_000_000,
            budget="high",
            query_timestamp=self.query_timestamp,
        )
        ranked_ids = []
        for item in result.results:
            document_id = item.document_id
            if document_id not in self.refs:
                raise ValueError("Hindsight returned a source outside the bound corpus")
            ranked_ids.append(self.refs[document_id])
            if len(ranked_ids) == 200:
                break
        return pack(self.store, ranked_ids, budget, profile="hindsight-raw")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            client = getattr(self, "client", None)
            if client is not None:
                client.close(stop_daemon=True)
        finally:
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if self._home_locked:
                self._home_locked = False
                _HINDSIGHT_HOME_LOCK.release()


@contextlib.contextmanager
def vendor_profile(name, store, now):
    if name not in PROFILES:
        raise ValueError("unsupported memory adapter")
    if name == "commontrace":
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="commontrace-vendor-") as directory:
        adapter_type = {
            "mem0-raw": Mem0Raw,
            "graphiti-episodic": GraphitiEpisodic,
            "hindsight-raw": HindsightRaw,
        }[name]
        adapter = adapter_type(directory, store, now)
        try:
            yield adapter
            if name == "hindsight-raw":
                for package, source_hash in adapter.descriptor["source_bindings"].items():
                    if package_source(package) != source_hash:
                        raise RuntimeError("vendor source changed during measurement")
            else:
                package = "mem0" if name == "mem0-raw" else "graphiti_core"
                if package_source(package) != adapter.descriptor["upstream_sha256"]:
                    raise RuntimeError("vendor source changed during measurement")
            if name in ("mem0-raw", "hindsight-raw") and model_artifact(adapter.descriptor["embedder"]) != adapter.descriptor["embedding_artifact"]:
                raise RuntimeError("vendor embedding artifacts changed during measurement")
        finally:
            adapter.close()
