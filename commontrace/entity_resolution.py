"""Entity resolution beyond lexical aliases: propose, and optionally merge, graph
entities that name one thing.

Similarity is the cosine of the entities' embeddings when an embedder is configured
(`kg_similarity.embedder`: a tag, a provider, or ``COMMONTRACE_GRAPH_EMBEDDER``),
else the Jaccard overlap of their names' character trigrams (stdlib), taking the
best pair of surface forms (name and recorded aliases). Pairs at or above
`threshold` are candidates, reported with their scores; `apply` merges only pairs
at or above the stricter `apply_threshold`, through `entities.merge`, with the
method and score kept as provenance. Entities of incompatible ontology types (neither
an ancestor of the other, and neither the untyped fallback) are reported as blocked
and never merged."""
from __future__ import annotations

from commontrace import kg_similarity

THRESHOLDS = {"embedding": (0.85, 0.95), "ngram": (0.6, 0.85)}
MAX_ENTITIES = 2000
SKIPPED_TYPES = frozenset({"lesson", "memory", "scope"})


def _forms(node) -> list[str]:
    aliases = [a for a in (node.properties or {}).get("aliases", [])[:20] if isinstance(a, str)]
    return list(dict.fromkeys([node.name or node.id.split(":", 1)[-1], *aliases]))


def compatible(onto, a: str, b: str) -> bool:
    """Whether entities typed `a` and `b` may be one: same lineage, or one is untyped."""
    from commontrace.ontology import FALLBACK_TYPE

    return FALLBACK_TYPE in (a, b) or a in onto.ancestors(b) or b in onto.ancestors(a)


def _candidates_nodes(root: str) -> list:
    from commontrace import graph

    nodes = [n for n in graph.load_nodes(root).values()
             if n.entity_type not in SKIPPED_TYPES and not n.is_forgotten
             and not (n.properties or {}).get("merged_into")]
    return sorted(nodes, key=lambda n: n.id)[:MAX_ENTITIES]


def _context(root: str, node_ids: list[str]) -> dict[str, str]:
    from commontrace import graph

    return {node_id: "; ".join(f"{n['relation']} {n['neighbor_name']}"
                               for n in graph.get_neighbors(root, node_id)[:8]) for node_id in node_ids}


def _ngram_scores(nodes) -> dict[tuple[int, int], float]:
    """Best trigram Jaccard over surface forms, only for pairs sharing a trigram."""
    grams = [[kg_similarity.ngrams(f) for f in _forms(n)] for n in nodes]
    postings: dict[str, set[int]] = {}
    for i, sets in enumerate(grams):
        for gram in set().union(*sets):
            postings.setdefault(gram, set()).add(i)
    pairs: set[tuple[int, int]] = set()
    for members in postings.values():
        if len(members) > 200:  # a trigram most names share says nothing
            continue
        ordered = sorted(members)
        pairs.update((a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:])
    return {(a, b): max(kg_similarity.jaccard(x, y) for x in grams[a] for y in grams[b]) for a, b in pairs}


def _embedding_scores(root: str, nodes, provider, context: bool) -> dict[tuple[int, int], float]:
    extra = _context(root, [n.id for n in nodes]) if context else {}
    texts = []
    for n in nodes:
        text = " / ".join(_forms(n))
        if extra.get(n.id):
            text += f" ({extra[n.id]})"
        texts.append(text)
    matrix = kg_similarity.pairwise(kg_similarity.embed(provider, texts, query=False))
    return {(a, b): matrix[a][b] for a in range(len(nodes)) for b in range(a + 1, len(nodes))}


def candidates(root: str, *, embedder=None, threshold: float | None = None, context: bool = False,
               limit: int = 100) -> dict:
    """Merge candidates, best first: {"method", "threshold", "pairs": [{a, b, score,
    a_type, b_type, compatible}]}."""
    from commontrace import ontology

    onto = ontology.load(root)
    nodes = _candidates_nodes(root)
    provider = kg_similarity.embedder(embedder)
    method = "embedding" if provider is not None else "ngram"
    floor = THRESHOLDS[method][0] if threshold is None else float(threshold)
    if not 0.0 < floor <= 1.0:
        raise ValueError("threshold must be in (0, 1]")
    scores = _embedding_scores(root, nodes, provider, context) if provider is not None and len(nodes) > 1 \
        else _ngram_scores(nodes) if provider is None else {}
    pairs = []
    for (a, b), score in scores.items():
        if score >= floor:
            x, y = nodes[a], nodes[b]
            pairs.append({"a": x.id, "b": y.id, "score": round(score, 4), "a_type": x.entity_type,
                          "b_type": y.entity_type, "compatible": compatible(onto, x.entity_type, y.entity_type)})
    pairs.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    return {"method": method, "threshold": floor, "entities": len(nodes), "pairs": pairs[:max(1, int(limit))]}


def _keeper(onto, x, y):
    """The node a merge keeps: the typed one, then the more mentioned, then the smaller id."""
    from commontrace.ontology import FALLBACK_TYPE

    def rank(n):
        return (n.entity_type == FALLBACK_TYPE, -len(onto.ancestors(n.entity_type)),
                -int((n.properties or {}).get("mentions", 0) or 0), n.id)

    return (x, y) if rank(x) <= rank(y) else (y, x)


def resolve(root: str, *, embedder=None, threshold: float | None = None, apply: bool = False,
            apply_threshold: float | None = None, context: bool = False, limit: int = 100) -> dict:
    """`candidates`, and with `apply`, merge each compatible pair at or above
    `apply_threshold`; an entity takes part in at most one merge per run."""
    from commontrace import entities, graph, ontology

    found = candidates(root, embedder=embedder, threshold=threshold, context=context, limit=limit)
    strict = THRESHOLDS[found["method"]][1] if apply_threshold is None else float(apply_threshold)
    if strict < found["threshold"]:
        raise ValueError("the apply threshold must not be below the reporting threshold")
    found.update(apply_threshold=strict, merged=[], skipped=[])
    if not apply:
        return found
    onto, nodes, touched = ontology.load(root), graph.load_nodes(root), set()
    for pair in found["pairs"]:
        if pair["score"] < strict:
            continue
        if not pair["compatible"]:
            found["skipped"].append({**pair, "reason": "incompatible ontology types"})
            continue
        if {pair["a"], pair["b"]} & touched:
            found["skipped"].append({**pair, "reason": "already merged in this run"})
            continue
        keep, dup = _keeper(onto, nodes[pair["a"]], nodes[pair["b"]])
        evidence = {"method": found["method"], "score": pair["score"], "apply_threshold": strict}
        out = entities.merge(root, keep.id, dup.id, evidence=evidence,
                             provenance={"source_path": "entity_resolution", "detail": evidence})
        touched.update((keep.id, dup.id))
        found["merged"].append({**out, **evidence})
    return found
