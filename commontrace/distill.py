from __future__ import annotations

import hashlib
import random
import re
from collections import Counter
from dataclasses import dataclass, field

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace.paths import STARTER_DOMAINS

_DOMAIN_SLUG_RE = re.compile(r"[^a-z0-9]+")

# --- Perf caps ---------------------------------------------------------------
# `find_clusters` used to compare every pair sharing a token: O(P) exact
# Jaccards where P approaches n^2 when one token is ubiquitous, and
# `representative` scored every pair inside each cluster: O(k^2) per cluster.
# Inputs at or below _DISTILL_EXACT_LIMIT take the original exact path
# (byte-identical clustering); above it, a MinHash/LSH pre-filter proposes a
# bounded candidate set that is verified with the same exact Jaccard + tag rule.
_DISTILL_EXACT_LIMIT = 500
_MINHASH_PERMS = 32
_MINHASH_BANDS = 16
_MINHASH_ROWS = 2
_BUCKET_EXACT_LIMIT = 64
_MAX_CANDIDATE_PAIRS = 50_000
_REPRESENTATIVE_LIMIT = 120
_MERSENNE_61 = (1 << 61) - 1


def _distill_permutations() -> tuple[tuple[int, int], ...]:
    rng = random.Random(0xD15711)
    return tuple((rng.randrange(1, _MERSENNE_61), rng.randrange(0, _MERSENNE_61)) for _ in range(_MINHASH_PERMS))


_DISTILL_PERMS = _distill_permutations()


def _distill_hash(token: str) -> int:
    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")


def _minhash_signature(tokens: set[str]) -> tuple[int, ...]:
    """MinHash signature over the same token sets `_jaccard` compares, so agreement estimates that Jaccard."""
    hashes = [_distill_hash(t) for t in tokens]
    return tuple(min((a * h + b) % _MERSENNE_61 for h in hashes) for a, b in _DISTILL_PERMS)


def _lsh_candidate_pairs(
    candidates: list[TraceCandidate],
    token_sets: dict[str, set[str]],
    threshold: float,
) -> list[tuple[str, str]]:
    """Bounded, deterministically ordered id-pairs to verify with exact Jaccard.

    LSH buckets (bands of a MinHash signature) replace the unbounded
    shared-token neighborhoods. Small buckets expand to all pairs; large
    buckets contribute hub-star + consecutive-chain edges so a big group of
    near-identical traces still connects transitively. Star edges sort first
    so truncation under `_MAX_CANDIDATE_PAIRS` keeps connectivity, not an
    arbitrary prefix of pairs. Complexity: O(n * _MINHASH_PERMS) signatures +
    O(capped) exact verifications by the caller.
    """
    del threshold  # verification applies the threshold; candidate generation is threshold-free LSH.
    signatures = {t.id: _minhash_signature(token_sets[t.id]) for t in candidates if token_sets[t.id]}
    buckets: dict[tuple[int, tuple[int, ...]], list[str]] = {}
    for tid in sorted(signatures):
        signature = signatures[tid]
        for band in range(_MINHASH_BANDS):
            key = (band, signature[band * _MINHASH_ROWS:(band + 1) * _MINHASH_ROWS])
            buckets.setdefault(key, []).append(tid)
    star: set[tuple[str, str]] = set()
    pairs: set[tuple[str, str]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        ordered = sorted(members)
        if len(ordered) <= _BUCKET_EXACT_LIMIT:
            for pos, left in enumerate(ordered):
                for right in ordered[pos + 1:]:
                    pairs.add((left, right))
        else:
            hub = ordered[0]
            for other in ordered[1:]:
                star.add((hub, other))
            for pos in range(len(ordered) - 1):
                star.add((ordered[pos], ordered[pos + 1]))
    ordered_pairs = sorted(star) + sorted(pairs - star)
    return ordered_pairs[:_MAX_CANDIDATE_PAIRS]


def _domain_slug(value: str) -> str:
    slug = _DOMAIN_SLUG_RE.sub("-", value.strip().lower()).strip("-")
    if not slug or slug.isdigit():
        return ""
    return slug[:48]


def _tokenize(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class TraceCandidate:
    id: str
    path: str
    title: str
    context_text: str
    solution_text: str
    tags: list[str]
    agent_type: str


@dataclass
class Cluster:
    traces: list[TraceCandidate] = field(default_factory=list)
    shared_terms: list[str] = field(default_factory=list)


def _already_curated_ids(existing_lessons_source_traces: list[list[str]]) -> set[str]:
    curated: set[str] = set()
    for source_list in existing_lessons_source_traces:
        curated.update(source_list)
    return curated


def find_clusters(
    traces: list[TraceCandidate],
    existing_lessons_source_traces: list[list[str]],
    similarity_threshold: float = 0.3,
    min_cluster_size: int = 2,
) -> list[Cluster]:
    curated_ids = _already_curated_ids(existing_lessons_source_traces)
    candidates = [t for t in traces if t.id not in curated_ids]
    by_id = {t.id: t for t in candidates}

    token_sets = {t.id: _tokenize(f"{t.title} {t.context_text}") for t in candidates}

    parent = {t.id: t.id for t in candidates}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    if similarity_threshold <= 0:
        for i in range(len(candidates) - 1):
            union(candidates[i].id, candidates[i + 1].id)
    elif len(candidates) <= _DISTILL_EXACT_LIMIT:
        token_index: dict[str, list[str]] = {}
        for t in candidates:
            for tok in token_sets[t.id]:
                token_index.setdefault(tok, []).append(t.id)

        compared: set[frozenset[str]] = set()
        for t in candidates:
            neighbor_ids: set[str] = set()
            for tok in token_sets[t.id]:
                neighbor_ids.update(token_index[tok])
            neighbor_ids.discard(t.id)
            for other_id in neighbor_ids:
                pair = frozenset((t.id, other_id))
                if pair in compared:
                    continue
                compared.add(pair)
                other = by_id[other_id]
                sim = _jaccard(token_sets[t.id], token_sets[other_id])
                tag_overlap = bool(set(t.tags) & set(other.tags))
                if sim >= similarity_threshold or (tag_overlap and sim >= similarity_threshold * 0.6):
                    union(t.id, other_id)
    else:
        tags_by_id = {t.id: set(t.tags) for t in candidates}
        for left_id, right_id in _lsh_candidate_pairs(candidates, token_sets, similarity_threshold):
            sim = _jaccard(token_sets[left_id], token_sets[right_id])
            tag_overlap = bool(tags_by_id[left_id] & tags_by_id[right_id])
            if sim >= similarity_threshold or (tag_overlap and sim >= similarity_threshold * 0.6):
                union(left_id, right_id)

    groups: dict[str, list[TraceCandidate]] = {}
    for t in candidates:
        groups.setdefault(find(t.id), []).append(t)

    clusters = []
    for group in groups.values():
        if len(group) < min_cluster_size:
            continue
        shared = set.intersection(*(token_sets[t.id] for t in group)) if group else set()
        clusters.append(Cluster(traces=group, shared_terms=sorted(shared)[:8]))

    clusters.sort(key=lambda c: len(c.traces), reverse=True)
    return clusters


def propose_domain(cluster: Cluster, agent_type: str) -> str:
    """A domain label for this candidate, from the cluster's own vocabulary."""
    starter = STARTER_DOMAINS.get(agent_type, [])
    cluster_tags = [t for trace in cluster.traces for t in trace.tags]
    tag_set = set(cluster_tags)
    for domain in starter:
        if domain in tag_set:
            return domain

    for candidate, _count in Counter(cluster_tags).most_common():
        slug = _domain_slug(candidate)
        if slug:
            return slug
    for term in cluster.shared_terms:
        slug = _domain_slug(term)
        if slug:
            return slug
    return "other"


def propose_tags(cluster: Cluster, max_tags: int = 8) -> list[str]:
    seen: list[str] = []
    for trace in cluster.traces:
        for tag in trace.tags:
            if tag not in seen:
                seen.append(tag)
    return seen[:max_tags]


def representative(cluster: Cluster) -> TraceCandidate:
    """The trace that best stands for the whole cluster -- its medoid.

    Medoid scoring is O(k^2) in cluster size, so clusters larger than
    `_REPRESENTATIVE_LIMIT` are scored on a deterministic sample (the first
    `_REPRESENTATIVE_LIMIT` traces in sorted id order). At or below the limit
    every trace is scored, exactly as before.
    """
    if len(cluster.traces) == 1:
        return cluster.traces[0]
    if len(cluster.traces) > _REPRESENTATIVE_LIMIT:
        members = sorted(cluster.traces, key=lambda t: t.id)[:_REPRESENTATIVE_LIMIT]
    else:
        members = cluster.traces
    tokens = [
        (t, set(_tokenize(f"{t.title} {t.context_text}")))
        for t in members
    ]
    best, best_score = cluster.traces[0], -1.0
    for trace, own in tokens:
        if not own:
            continue
        score = sum(
            len(own & other) / len(own | other)
            for candidate, other in tokens
            if candidate.id != trace.id and (own | other)
        )
        if score > best_score or (score == best_score and trace.id < best.id):
            best, best_score = trace, score
    return best


def variants(texts: list[str], limit: int = 4) -> list[tuple[str, int]]:
    """Distinct versions of a repeated field, most common first, with counts."""
    groups: dict[str, tuple[str, int]] = {}
    for text in texts:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            continue
        key = cleaned.lower().rstrip(".!? ")
        original, count = groups.get(key, (cleaned, 0))
        groups[key] = (original, count + 1)
    ranked = sorted(groups.values(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:limit]


def propose_description(cluster: Cluster) -> str:
    """A sentence a human can read, not a bag of words."""
    n = len(cluster.traces)
    title = (representative(cluster).title or "").strip()
    if not title:
        shared = ", ".join(cluster.shared_terms[:5]) or "(no strongly shared terms)"
        return f"Candidate ({n} traces): repeated pattern around {shared}"
    return f"{title} — and {n - 1} more like it" if n > 1 else title
