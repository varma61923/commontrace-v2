from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace.paths import STARTER_DOMAINS

_DOMAIN_SLUG_RE = re.compile(r"[^a-z0-9]+")


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
    else:
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
    """The trace that best stands for the whole cluster -- its medoid."""
    if len(cluster.traces) == 1:
        return cluster.traces[0]
    tokens = [
        (t, set(_tokenize(f"{t.title} {t.context_text}")))
        for t in cluster.traces
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
