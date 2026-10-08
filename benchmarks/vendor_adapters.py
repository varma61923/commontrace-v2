"""Optional executable local vendor profiles; never substitutes CommonTrace recall.

These raw-source profiles deliberately exclude managed APIs and LLM extraction.
Gold evidence and answers never enter them. Canonical Store is only a source map.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import importlib.metadata
import os
import tempfile

from benchmarks.artifacts import model_artifact, package_source
from commontrace.conversation.search import Recall, tokens
from commontrace.conversation.timeparse import parse_moment

PROFILES = ("commontrace", "mem0-raw", "graphiti-episodic")


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
                node = EpisodicNode(name=str(turn.ref), group_id=store.space,
                    source=EpisodeType.message, source_description="raw conversation turn",
                    content=f"{turn.speaker}: {turn.text}",
                    valid_at=(turn.at or dt.datetime(1970, 1, 1)).replace(tzinfo=dt.timezone.utc))
                self.loop.run_until_complete(node.save(self.driver))
                self.refs[node.uuid] = turn.id
            self.descriptor = {"profile": "graphiti-episodic",
                "version": version, "upstream_sha256": source_binding, "inference": False,
                "search": "native-episode-bm25", "reranker": "rrf", "top_k": 200,
                "backend": "falkordblite", "time_filter": "history-cutoff-before-indexing",
                "packing": "whole-source-turns", "entity_graph": False}
        except BaseException:
            self.close()
            raise

    def retrieve(self, question, budget):
        from graphiti_core.search.search import episode_search
        from graphiti_core.search.search_config import EpisodeSearchConfig, EpisodeSearchMethod
        from graphiti_core.search.search_filters import SearchFilters

        episodes, _scores = self.loop.run_until_complete(episode_search(
            self.driver, None, question, [], [self.store.space],
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


@contextlib.contextmanager
def vendor_profile(name, store, now):
    if name not in PROFILES:
        raise ValueError("unsupported memory adapter")
    if name == "commontrace":
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="commontrace-vendor-") as directory:
        adapter = (Mem0Raw if name == "mem0-raw" else GraphitiEpisodic)(directory, store, now)
        try:
            yield adapter
            package = "mem0" if name == "mem0-raw" else "graphiti_core"
            if package_source(package) != adapter.descriptor["upstream_sha256"]:
                raise RuntimeError("vendor source changed during measurement")
            if name == "mem0-raw" and model_artifact(adapter.descriptor["embedder"]) != adapter.descriptor["embedding_artifact"]:
                raise RuntimeError("vendor embedding artifacts changed during measurement")
        finally:
            adapter.close()
