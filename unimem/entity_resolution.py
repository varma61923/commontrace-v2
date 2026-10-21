"""Entity resolution beyond lexical aliases: propose, and optionally merge, graph
entities that name one thing.

Similarity is the cosine of the entities' embeddings when an embedder is configured
(`kg_similarity.embedder`: a tag, a provider, or ``UNIMEM_GRAPH_EMBEDDER``),
else the Jaccard overlap of their names' character trigrams (stdlib), taking the
best pair of surface forms (name and recorded aliases). Pairs at or above
`threshold` are candidates, reported with their scores; `apply` merges only pairs
at or above the stricter `apply_threshold`, through `entities.merge`, with the
method and score kept as provenance. Entities of incompatible ontology types (neither
an ancestor of the other, and neither the untyped fallback) are reported as blocked
and never merged."""
from __future__ import annotations

from dataclasses import dataclass

from unimem import kg_similarity

THRESHOLDS = {"embedding": (0.85, 0.95), "ngram": (0.6, 0.85)}
MAX_ENTITIES = 2000
SKIPPED_TYPES = frozenset({"lesson", "memory", "scope"})
TIERED_THRESHOLDS = {"entropy": 2.0, "minhash": 0.8, "jaccard": 0.8, "cosine": 0.6, "top_k": 15}


def _forms(node) -> list[str]:
    aliases = [a for a in (node.properties or {}).get("aliases", [])[:20] if isinstance(a, str)]
    return list(dict.fromkeys([node.name or node.id.split(":", 1)[-1], *aliases]))


def compatible(onto, a: str, b: str) -> bool:
    """Whether entities typed `a` and `b` may be one: same lineage, or one is untyped."""
    from unimem.ontology import FALLBACK_TYPE

    return FALLBACK_TYPE in (a, b) or a in onto.ancestors(b) or b in onto.ancestors(a)


def _candidates_nodes(root: str) -> list:
    from unimem import graph

    nodes = [n for n in graph.load_nodes(root).values()
             if n.entity_type not in SKIPPED_TYPES and not n.is_forgotten
             and not (n.properties or {}).get("merged_into")]
    return sorted(nodes, key=lambda n: n.id)[:MAX_ENTITIES]


def _context(root: str, node_ids: list[str]) -> dict[str, str]:
    from unimem import graph

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


@dataclass(frozen=True)
class _TieredSurface:
    forms: frozenset[str]
    grams: frozenset[str]
    entropy: float
    signature: list[int]


def _tiered_surface(forms: list[str]) -> _TieredSurface:
    """Precompute the exact, entropy, MinHash and trigram signals for one entity."""
    import math
    from collections import Counter

    from unimem.overlap import minhash

    normalized = frozenset(value for form in forms if (value := kg_similarity.normalize(form)))
    grams = frozenset().union(*(kg_similarity.ngrams(form) for form in normalized)) if normalized else frozenset()
    text = " ".join(sorted(normalized))
    counts = Counter(text)
    total = max(1, len(text))
    entropy = -sum((count / total) * math.log2(count / total) for count in counts.values())
    signature_text = " ".join(value.replace(" ", "_") for value in sorted(grams))
    signature = minhash(signature_text) if grams else []
    return _TieredSurface(normalized, grams, entropy, signature)


def tiered_pair_signals(a_forms: list[str], b_forms: list[str]) -> dict[str, float | bool]:
    """Deterministic signals used by tiered entity resolution and its labeled eval.

    This reports scores without making a decision. The caller applies entropy and
    similarity floors, and exact aliases always remain the first tier.
    """
    from unimem.overlap import estimate_jaccard

    a, b = _tiered_surface(a_forms), _tiered_surface(b_forms)
    minhash_score = estimate_jaccard(a.signature, b.signature) if a.signature and b.signature else 0.0
    return {
        "exact": bool(a.forms & b.forms),
        "entropy": min(a.entropy, b.entropy),
        "minhash": minhash_score,
        "jaccard": kg_similarity.jaccard(a.grams, b.grams),
    }


def _tiered_exact_pairs(surfaces: list[_TieredSurface]) -> set[tuple[int, int]]:
    """Index exact normalized forms instead of comparing every entity pair."""
    from collections import defaultdict

    by_form: dict[str, list[int]] = defaultdict(list)
    for index, surface in enumerate(surfaces):
        for form in surface.forms:
            by_form[form].append(index)

    pairs: set[tuple[int, int]] = set()
    for members in by_form.values():
        for offset, a in enumerate(members):
            for b in members[offset + 1:]:
                pairs.add((a, b))
    return pairs


def _tiered_minhash_pairs(
    surfaces: list[_TieredSurface], *, entropy_threshold: float, minhash_threshold: float
) -> set[tuple[int, int]]:
    """Return lossless MinHash candidates for the current threshold using LSH bands.

    With the default 128-position signature and four-position bands, a pair with
    more than 96 agreeing positions must share a complete band. Thus for
    thresholds above 0.75 the index cannot omit a pair that passes the existing
    MinHash predicate. Lower thresholds use the exhaustive high-entropy pair
    set because this guarantee no longer holds.
    """
    from collections import defaultdict

    from unimem.overlap import DEFAULT_NUM_PERM

    eligible = [
        index for index, surface in enumerate(surfaces)
        if surface.entropy >= entropy_threshold
    ]
    if len(eligible) < 2:
        return set()

    signatures_match_expected = all(
        len(surfaces[index].signature) == DEFAULT_NUM_PERM for index in eligible
    )
    if minhash_threshold <= 0.75 or not signatures_match_expected:
        return {
            (a, b)
            for offset, a in enumerate(eligible)
            for b in eligible[offset + 1:]
        }

    band_size = 4
    buckets: dict[tuple[int, tuple[int, ...]], list[int]] = defaultdict(list)
    pairs: set[tuple[int, int]] = set()
    for index in eligible:
        signature = surfaces[index].signature
        for band_index, start in enumerate(range(0, DEFAULT_NUM_PERM, band_size)):
            band = tuple(signature[start:start + band_size])
            key = (band_index, band)
            for other in buckets[key]:
                pairs.add((other, index))
            buckets[key].append(index)
    return pairs


def candidates(root: str, *, embedder=None, threshold: float | None = None, context: bool = False,
               limit: int = 100) -> dict:
    """Merge candidates, best first: {"method", "threshold", "pairs": [{a, b, score,
    a_type, b_type, compatible}]}."""
    from unimem import ontology

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
    from unimem.ontology import FALLBACK_TYPE

    def rank(n):
        return (n.entity_type == FALLBACK_TYPE, -len(onto.ancestors(n.entity_type)),
                -int((n.properties or {}).get("mentions", 0) or 0), n.id)

    return (x, y) if rank(x) <= rank(y) else (y, x)


def resolve(root: str, *, embedder=None, threshold: float | None = None, apply: bool = False,
            apply_threshold: float | None = None, context: bool = False, limit: int = 100,
            tiered: bool = False, complete=None) -> dict:
    """`candidates`, and with `apply`, merge each compatible pair at or above
    `apply_threshold`; an entity takes part in at most one merge per run."""
    from unimem import entities, graph, ontology

    found = (tiered_candidates(root, embedder=embedder, complete=complete, limit=limit) if tiered
             else candidates(root, embedder=embedder, threshold=threshold, context=context, limit=limit))
    found.setdefault('threshold', .6)
    strict = THRESHOLDS.get(found["method"], (.6, .95))[1] if apply_threshold is None else float(apply_threshold)
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


def tiered_candidates(root: str, *, embedder=None, complete=None, limit=100,
                      entropy_threshold: float = TIERED_THRESHOLDS["entropy"],
                      minhash_threshold: float = TIERED_THRESHOLDS["minhash"],
                      jaccard_threshold: float = TIERED_THRESHOLDS["jaccard"],
                      cosine_threshold: float = TIERED_THRESHOLDS["cosine"],
                      top_k: int = TIERED_THRESHOLDS["top_k"]) -> dict:
    """Exact aliases → entropy-gated MinHash → top-15 cosine → one batched judge.

    Suggestions remain reviewable; ontology compatibility still gates merging.
    Thresholds are explicit and require task-specific calibration, not claimed
    universally correct from a static graph or a competitor's defaults.
    """
    import math

    from unimem import llm, ontology
    from unimem.llm_runtime import purpose
    from unimem.overlap import estimate_jaccard

    for name, value in (("minhash_threshold", minhash_threshold), ("jaccard_threshold", jaccard_threshold),
                        ("cosine_threshold", cosine_threshold)):
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0, 1]")
    if not math.isfinite(entropy_threshold) or entropy_threshold < 0:
        raise ValueError("entropy_threshold must be finite and non-negative")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")

    nodes = _candidates_nodes(root)
    onto = ontology.load(root)
    found, remainder = {}, {}
    surfaces = [_tiered_surface(_forms(node)) for node in nodes]

    for a, b in _tiered_exact_pairs(surfaces):
        if compatible(onto, nodes[a].entity_type, nodes[b].entity_type):
            found[(a, b)] = ("exact", 1.0)

    for a, b in _tiered_minhash_pairs(
        surfaces, entropy_threshold=entropy_threshold, minhash_threshold=minhash_threshold
    ):
        if (a, b) in found or not compatible(onto, nodes[a].entity_type, nodes[b].entity_type):
            continue
        similarity = (
            estimate_jaccard(surfaces[a].signature, surfaces[b].signature)
            if surfaces[a].signature and surfaces[b].signature else 0.0
        )
        jaccard = kg_similarity.jaccard(surfaces[a].grams, surfaces[b].grams)
        if similarity >= minhash_threshold and jaccard >= jaccard_threshold:
            found[(a, b)] = ("minhash", similarity)
    provider = kg_similarity.embedder(embedder)
    if provider is not None:
        scores = _embedding_scores(root, nodes, provider, False)
        for a in range(len(nodes)):
            candidates = sorted(
                [(b, score) for (x, b), score in scores.items() if x == a and score >= cosine_threshold],
                key=lambda pair: (-pair[1], nodes[pair[0]].id),
            )[:top_k]
            for b, score in candidates:
                if (a, b) not in found and compatible(onto, nodes[a].entity_type, nodes[b].entity_type):
                    remainder[(a, b)] = score
    if remainder and complete is not None:
        offered = [
            {"a": nodes[a].id, "b": nodes[b].id, "a_names": sorted(surfaces[a].forms),
             "b_names": sorted(surfaces[b].forms)}
            for a, b in list(remainder)[: max(1, min(limit, 100))]
        ]
        with purpose("entity-dedup"):
            text, _ = complete(
                'Return JSON {"duplicates":[["a-id","b-id"]]}. '
                "Only identify aliases of the same real entity; prefer no merge for ambiguity.\n"
                + __import__("json").dumps(offered)
            )
        judged = llm._extract_json_object(text).get("duplicates", [])
        accepted = {
            tuple(pair)
            for pair in judged
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(identity, str) for identity in pair)
        }
        for (a, b), score in remainder.items():
            if (nodes[a].id, nodes[b].id) in accepted:
                found[(a, b)] = ("llm", score)
    pairs = [
        {
            "a": nodes[a].id,
            "b": nodes[b].id,
            "score": score,
            "tier": tier,
            "a_type": nodes[a].entity_type,
            "b_type": nodes[b].entity_type,
            "compatible": True,
        }
        for (a, b), (tier, score) in found.items()
    ]
    return {
        "method": "tiered",
        "entities": len(nodes),
        "pairs": sorted(pairs, key=lambda pair: (-pair["score"], pair["a"], pair["b"]))[:limit],
        "llm_batches": int(bool(remainder and complete)),
        "thresholds": {"entropy": entropy_threshold, "minhash": minhash_threshold,
                       "jaccard": jaccard_threshold, "cosine": cosine_threshold, "top_k": top_k},
    }
