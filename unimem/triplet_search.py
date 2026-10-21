"""Semantic triplet search over the knowledge graph: (subject, relation, object) ranked as units.

Fact search ranks sentences; this ranks *relations*. A question such as "who
leads billing?" is answered by the edge ``Dana --leads--> Billing`` even when no
stored sentence phrases it that way. Each active edge is scored as a whole:

    distance = d(query, subject) + d(query, relation) + d(query, object)

where an element that does not match the query at all costs a fixed penalty
instead of its raw distance. A triplet therefore has to match on more than one
element to rank well, and one strongly matching endpoint cannot carry an
irrelevant relation. This is Cognee's triplet importance (summed node and edge
vector distances with ``triplet_distance_penalty`` = 6.5, Apache-2.0), cited and
reimplemented, not vendored.

Similarity is the cosine of embeddings when a knowledge-graph embedder is
configured (`kg_similarity.embedder`: an argument, or ``UNIMEM_GRAPH_EMBEDDER``),
else a stdlib measure: how much of the element's character trigrams and words
the query contains. Nothing loads a model or calls a network unless an embedder
is configured.

Admission mirrors fact search. An edge is returned only when it is active at the
requested valid and record time; when an endpoint is a fact, the caller may see
that fact; and when the relation was learned from a fact (its provenance names
``fact:<id>``), the caller may see that fact too, so a relation extracted from a
private fact cannot disclose it. Edges between plain entities from other sources
follow the graph's own visibility, as graph traversal in `search_recipes` does.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from unimem import graph, kg_similarity, lesson_cache, search_recipes
from unimem._lexical import STOPWORDS

PENALTY = 6.5           # Cognee's triplet_distance_penalty for an element with no match
MATCH_FLOOR = 0.34      # below this similarity an element counts as not matching
MAX_EDGES = 50_000      # edges scanned per query
MAX_EMBED_TEXTS = 4_000 # distinct element texts embedded per query
MAX_TOP_K = 100
_WORD = re.compile(r"\w+", re.UNICODE)


def _words(text: str) -> set[str]:
    return {word for word in _WORD.findall(unicodedata.normalize("NFKC", text or "").casefold())
            if word not in STOPWORDS and len(word) > 1}


def lexical_similarity(query: str, element: str) -> float:
    """How much of `element` the query contains: the better of trigram and word containment."""
    grams = kg_similarity.ngrams(element)
    words = _words(element)
    if not grams and not words:
        return 0.0
    query_grams = kg_similarity.ngrams(query)
    query_words = _words(query)
    gram_part = len(grams & query_grams) / len(grams) if grams else 0.0
    word_part = len(words & query_words) / len(words) if words else 0.0
    return max(gram_part, word_part)


def _distance(similarity: float) -> float:
    return PENALTY if similarity < MATCH_FLOOR else 1.0 - similarity


def _fact_id(node_id: str, node) -> str | None:
    if node_id.startswith("memory:"):
        return node_id.removeprefix("memory:")
    if node is not None and node.entity_type == "memory":
        return node_id
    return None


def _label(node_id: str, node) -> str:
    return (node.name if node is not None and node.name else node_id.split(":", 1)[-1]).strip()


def search(root: str, query: str, *, top_k: int = 10, context: list[str] | None = None, scope: str = "",
           as_of: str | None = None, known_at: str | None = None, action_class: str = "",
           relations: list[str] | None = None, embedder=None, explain: dict | None = None) -> list[dict[str, Any]]:
    """The `top_k` triplets nearest to `query`, best first, each with its score and provenance."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be an integer in 1..{MAX_TOP_K}")
    if relations is not None and (not isinstance(relations, list) or not all(isinstance(r, str) for r in relations)):
        raise ValueError("relations must be a list of relation names")
    wanted = set(relations) if relations else None
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    known = lesson_cache.parse_moment(known_at) if known_at else None
    current_at = datetime.now(timezone.utc) if moment is None and known is None else None
    index = graph._full_index(root)
    nodes = index.nodes

    candidates = []
    for edge in index.edges[:MAX_EDGES]:
        if wanted is not None and edge.relation not in wanted:
            continue
        source, target = nodes.get(edge.source), nodes.get(edge.target)
        if (source is not None and source.is_forgotten) or (target is not None and target.is_forgotten):
            continue
        if not graph._is_active_edge(edge, moment, known, current_at=current_at):
            continue
        candidates.append((edge, _label(edge.source, source), edge.relation.replace("_", " "),
                           _label(edge.target, target)))
    if not candidates:
        return []

    provider = kg_similarity.embedder(embedder)
    texts = list(dict.fromkeys(text for _edge, *parts in candidates for text in parts))
    similarity: dict[str, float] = {}
    method = "lexical"
    if provider is not None and len(texts) <= MAX_EMBED_TEXTS:
        query_vector = kg_similarity.embed(provider, [query], query=True)[0]
        for text, vector in zip(texts, kg_similarity.embed(provider, texts, query=False)):
            similarity[text] = max(0.0, kg_similarity.cosine(query_vector, vector))
        method = "embedding"
    else:
        similarity = {text: lexical_similarity(query, text) for text in texts}

    scored = []
    for edge, subject, relation, obj in candidates:
        parts = [similarity[subject], similarity[relation], similarity[obj]]
        if max(parts) < MATCH_FLOOR:
            continue
        distance = sum(_distance(value) for value in parts)
        scored.append((distance, -float(edge.weight), edge.source, edge.relation, edge.target,
                       edge, subject, relation, obj, parts))
    scored.sort(key=lambda item: item[:5])

    # Admit facts behind the best candidates; scan further only as rows are refused.
    out: list[dict[str, Any]] = []
    effective = context if context is not None else ([scope] if scope else [])
    position = 0
    while len(out) < top_k and position < len(scored):
        window = scored[position:position + top_k * 4]
        position += len(window)
        needed: set[str] = set()
        for *_key, edge, _s, _r, _o, _p in window:
            for node_id in (edge.source, edge.target):
                fact = _fact_id(node_id, nodes.get(node_id))
                needed.add(fact if fact is not None else node_id)
            origin = str((edge.properties or {}).get("source", ""))
            if origin.startswith("fact:"):
                needed.add(origin.removeprefix("fact:"))
        existing: set[str] = set()
        admitted = search_recipes.admit(root, needed, context=effective, as_of=as_of, known_at=known_at,
                                        action_class=action_class, existing=existing)
        for distance, _weight, _s_id, _rel, _t_id, edge, subject, relation, obj, parts in window:
            refused = False
            for node_id in (edge.source, edge.target):
                fact = _fact_id(node_id, nodes.get(node_id))
                candidate = fact if fact is not None else node_id
                if (fact is not None or candidate in existing) and candidate not in admitted:
                    refused = True
            origin = str((edge.properties or {}).get("source", ""))
            if origin.startswith("fact:") and origin.removeprefix("fact:") not in admitted:
                refused = True
            if refused:
                continue
            properties = edge.properties or {}
            out.append({
                "subject": {"id": edge.source, "name": subject,
                            "type": getattr(nodes.get(edge.source), "entity_type", "")},
                "relation": edge.relation,
                "object": {"id": edge.target, "name": obj,
                           "type": getattr(nodes.get(edge.target), "entity_type", "")},
                "text": f"{subject} {relation} {obj}",
                "score": 1.0 / (1.0 + distance),
                "distance": distance,
                "similarity": {"subject": parts[0], "relation": parts[1], "object": parts[2]},
                "weight": float(edge.weight),
                "valid_at": edge.valid_at, "invalid_at": edge.invalid_at,
                "evidence": str(properties.get("evidence", "") or ""),
                "source": str(properties.get("source", "") or ""),
            })
            if len(out) >= top_k:
                break
    if explain is not None:
        explain.update(method=method, edges_scanned=len(candidates), matched=len(scored),
                       penalty=PENALTY, match_floor=MATCH_FLOOR)
    for row in out:
        if not math.isfinite(row["score"]):
            raise ValueError("triplet scores must be finite")
    return out
