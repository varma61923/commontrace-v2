"""Named, inspectable retrieval recipes over the canonical governed fact store.

Dense scores are optional, supplied by a local embedding adapter. Filters and
proof admission run before ranking; unavailable signals are never fabricated.
"""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from commontrace import graph, hierarchical, lesson_cache


@dataclass(frozen=True)
class SearchRecipe:
    name: str
    dense: float = 0.45
    lexical: float = 0.4
    entity: float = 0.1
    temporal: float = 0.05
    utility: float = 0.0
    reranker: str = "score"
    mmr_lambda: float = 0.7
    half_life_days: float = 90.0


RECIPES = {
    "balanced": SearchRecipe("balanced"),
    "diverse": SearchRecipe("diverse", reranker="mmr"),
    "nearby": SearchRecipe("nearby", reranker="node-distance"),
    "corroborated": SearchRecipe("corroborated", reranker="episode-mentions"),
    "recent": SearchRecipe("recent", temporal=0.25, half_life_days=14),
    "decision": SearchRecipe("decision", utility=0.2),
}


class GraphBackend(Protocol):
    def neighbors(self, node_id: str, *, as_of: str | None = None) -> list[dict]: ...


class LocalGraph:
    def __init__(self, root: str):
        self.root = root

    def neighbors(self, node_id: str, *, as_of: str | None = None) -> list[dict]:
        return graph.get_neighbors(self.root, node_id, as_of=as_of)


def terms(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def sigmoid_bm25(raw: float, query_terms: int) -> float:
    """Query-length calibrated, bounded logistic; a non-match has zero score."""
    if raw <= 0:
        return 0.0
    x = max(-60.0, min(60.0, raw / max(1, query_terms) - 1))
    return 1 / (1 + math.exp(-x))


def _bm25(query: list[str], documents: list[list[str]]) -> list[float]:
    count = len(documents)
    average = sum(map(len, documents)) / max(1, count)
    frequencies = Counter(t for d in documents for t in set(d))
    scores = []
    for doc in documents:
        tf = Counter(doc)
        norm = 1.2 * (0.25 + 0.75 * len(doc) / max(1, average))
        raw = sum(math.log(1 + (count - frequencies[t] + 0.5) / (frequencies[t] + 0.5))
                  * tf[t] * 2.2 / (tf[t] + norm) for t in set(query) if tf[t])
        scores.append(sigmoid_bm25(raw, len(set(query))))
    return scores


def _distances(backend: GraphBackend, center: str, as_of: str | None) -> dict[str, int]:
    distances = {center: 0}
    queue = deque([center])
    while queue and len(distances) < 2000:
        current = queue.popleft()
        if distances[current] >= 4:
            continue
        for row in backend.neighbors(current, as_of=as_of):
            nid = row["neighbor_id"]
            if nid not in distances:
                distances[nid] = distances[current] + 1
                queue.append(nid)
    return distances


def search(root: str, query: str, *, recipe: str = "balanced", scope: str = "", limit: int = 10,
           as_of: str | None = None, dense_scores: dict[str, float] | None = None,
           entity_ids: Sequence[str] = (), center: str = "", backend: GraphBackend | None = None,
           utility: dict[str, float] | None = None, context: list[str] | None = None) -> list[dict[str, Any]]:
    if recipe not in RECIPES:
        raise ValueError(f"unknown search recipe: {recipe}")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 1000:
        raise ValueError("limit must be an integer in 0..1000")
    cfg = RECIPES[recipe]
    if recipe == "decision" and utility is None:
        from commontrace.causal_policy import utility_priors

        utility = utility_priors(root)
    reference = lesson_cache.parse_moment(as_of) if as_of else datetime.now(timezone.utc)
    facts = hierarchical.list_facts(root, scope=scope, as_of=reference.isoformat())
    if as_of is None:
        facts = [f for f in facts if f.status == "active"]
    if context is not None:
        from commontrace.memory_control import matches

        facts = [f for f in facts if matches(f.scopes, context)]
    documents = [terms(f.statement) for f in facts]
    sparse = _bm25(terms(query), documents)
    backend = backend or LocalGraph(root)
    nearby = _distances(backend, center, as_of) if center else {}
    entity_memories = {r["neighbor_id"] for entity in entity_ids for r in backend.neighbors(entity, as_of=as_of)}
    rows = []
    for fact, lexical in zip(facts, sparse):
        dense = float((dense_scores or {}).get(fact.id, 0))
        if not math.isfinite(dense) or not -1 <= dense <= 1:
            raise ValueError("dense cosine scores must be finite in [-1, 1]")
        dense = max(0.0, dense)
        entity = float(fact.id in entity_memories or f"memory:{fact.id}" in entity_memories)
        if not lexical and not dense and not entity:
            continue
        age = max(0.0, (reference - lesson_cache.parse_moment(fact.valid_from)).total_seconds() / 86400)
        temporal = math.exp(-math.log(2) * age / cfg.half_life_days)
        prior = float((utility or {}).get(fact.id, 0))
        if not math.isfinite(prior) or not -1 <= prior <= 1:
            raise ValueError("causal utility must be finite in [-1, 1]")
        signals = {"dense": dense, "lexical": lexical, "entity": entity,
                   "temporal": temporal, "utility": prior}
        # Keep absence of an embedding service from depressing every score.
        denominator = cfg.lexical + cfg.entity + cfg.temporal + (cfg.dense if dense_scores is not None else 0)
        score = sum(getattr(cfg, key) * value for key, value in signals.items()) / denominator
        rows.append({"id": fact.id, "text": fact.statement, "score": score, "signals": signals,
                     "source_traces": fact.source_traces, "valid_from": fact.valid_from,
                     "recorded_at": fact.created_at, "stability": fact.stability,
                     "distance": nearby.get(fact.id, nearby.get(f"memory:{fact.id}", 999))})
    rows.sort(key=lambda r: (-r["score"], r["id"]))
    if cfg.reranker == "node-distance":
        if not center:
            raise ValueError("nearby recipe requires a center node")
        rows.sort(key=lambda r: (r["distance"], -r["score"], r["id"]))
    elif cfg.reranker == "episode-mentions":
        rows.sort(key=lambda r: (-len(set(r["source_traces"])), -r["score"], r["id"]))
    elif cfg.reranker == "mmr":
        selected = []
        pool = rows[:max(limit * 10, 100)]
        while pool and len(selected) < limit:
            def mmr(row):
                a = set(terms(row["text"]))
                redundancy = max((len(a & b) / max(1, len(a | b))
                                  for b in (set(terms(s["text"])) for s in selected)), default=0)
                return cfg.mmr_lambda * row["score"] - (1 - cfg.mmr_lambda) * redundancy
            chosen = max(pool, key=mmr)
            selected.append(chosen)
            pool.remove(chosen)
        rows = selected
    return rows[:limit]


Retriever = Callable[..., list[dict]]


class RetrieverRegistry:
    """Explicit plug-in surface; custom retrievers receive the same caller filters."""
    def __init__(self):
        self._items: dict[str, Retriever] = {}

    def register(self, name: str, retriever: Retriever) -> None:
        if name in self._items:
            raise ValueError(f"retriever already registered: {name}")
        self._items[name] = retriever

    def retrieve(self, name: str, root: str, query: str, **options) -> list[dict]:
        if name not in self._items:
            raise ValueError(f"unknown retriever: {name}")
        return self._items[name](root, query, **options)

    def names(self) -> list[str]:
        return sorted(self._items)


def decomposition(root: str, query: str, **options) -> list[dict]:
    hits: dict[str, dict] = {}
    for part in re.split(r"\s+(?:and|then)\s+|[;?]", query)[:8]:
        for row in search(root, part, **options):
            if row["id"] not in hits or row["score"] > hits[row["id"]]["score"]:
                hits[row["id"]] = row
    return sorted(hits.values(), key=lambda r: (-r["score"], r["id"]))[:options.get("limit", 10)]


def graph_completion(root: str, query: str, **options) -> list[dict]:
    """Expand entity seeds using bounded graph walks; emits evidence, no hidden CoT."""
    entities = graph.extract_entities_from_text(root, query)
    return search(root, query, entity_ids=entities, **options)


def nl_query(root: str, query: str, **options) -> list[dict]:
    """Local NL-to-filter adapter: category:<name> and ordinary free text."""
    match = re.search(r"\bcategory:(\w+)", query)
    category = match[1] if match else ""
    if category and category not in hierarchical.CATEGORIES:
        raise ValueError("unknown fact category")
    text = re.sub(r"\bcategory:\w+", "", query)
    limit = options.pop("limit", 10)
    rows = search(root, text, limit=1000 if category else limit, **options)
    if category:
        eligible = {f.id for f in hierarchical.list_facts(root, category=category,
                    scope=options.get("scope", ""), as_of=options.get("as_of"))}
        rows = [r for r in rows if r["id"] in eligible][:limit]
    return rows


REGISTRY = RetrieverRegistry()
for _name, _retriever in (("hybrid", search), ("decomposition", decomposition),
                         ("graph-completion", graph_completion), ("nl-query", nl_query)):
    REGISTRY.register(_name, _retriever)


def recipe_manifest() -> dict:
    return {name: asdict(recipe) for name, recipe in RECIPES.items()}
