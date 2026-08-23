"""The generic Curator: `commontrace distill` finds repeated patterns across
memory/traces/ and proposes candidate lessons, closing the "Finds repeated
failure patterns / Extracts candidate lessons" half of the pitch for any
agent_type -- not just the code-review profile's Omega, which only runs
inside a Claude Code session driving SKILL.md's double-review pipeline.

This is deliberately a *heuristic* clustering (word-overlap Jaccard
similarity over trace title+context_text, no LLM call, no external API key)
rather than an LLM-based distillation: a headless CLI command has no
standing agent session to call out to, and baking in a hardcoded model
call/API-key dependency here would be a much bigger, riskier addition than
this pass is scoped for. What it produces is a *candidate*, at
`status: review` -- never `active` -- so it is exactly as trustworthy as
the human/Validator who runs `commontrace lesson approve` on it, matching
"Validates and approves lessons" as a distinct, deliberate step (see
commontrace/commands/lesson_cmd.py's run_approve/run_reject).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from commontrace.paths import STARTER_DOMAINS

# Unicode-aware -- see commontrace/retrieval.py for why the ASCII-only
# class was wrong. Clustering non-English traces produced 0.0 Jaccard for
# every pair, so `distill` reported "no repeated patterns" on a corpus full
# of them.
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = frozenset(
    """
    a an the of to in on for with and or but is are was were be been being
    this that these those it its as at by from into over under again
    further then once here there when where why how all any both each
    few more most other some such no nor not only own same so than too
    very can will just don should now i you he she we they them his her
    """.split()
)


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
    """Union-find over pairwise Jaccard similarity of (title + context_text)
    tokens, restricted to traces not already referenced by an existing
    lesson's source_traces (so re-running distill doesn't keep re-proposing
    patterns a human already curated)."""
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

    # A full pairwise scan is O(n^2) Jaccard computations, which made
    # `distill` unusable on trace stores of any real size. Every union
    # condition below requires sim > 0 -- the tag-overlap relaxation still
    # gates on `sim >= similarity_threshold * 0.6`, and _jaccard returns 0.0
    # whenever the two token sets don't intersect -- so a pair sharing no
    # token can never trigger a union for any threshold > 0. That makes an
    # inverted token->trace-id index a behavior-preserving pre-filter: only
    # pairs sharing at least one token are ever compared. (threshold <= 0 is
    # the degenerate "cluster everything" case, where every pair trivially
    # qualifies regardless of token overlap, so it's handled separately
    # rather than pretending the index still applies.)
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
    starter = STARTER_DOMAINS.get(agent_type, STARTER_DOMAINS["custom"])
    cluster_tags = {t for trace in cluster.traces for t in trace.tags}
    for domain in starter:
        if domain in cluster_tags:
            return domain
    return starter[-1] if starter else "other"


def propose_tags(cluster: Cluster, max_tags: int = 8) -> list[str]:
    seen: list[str] = []
    for trace in cluster.traces:
        for tag in trace.tags:
            if tag not in seen:
                seen.append(tag)
    return seen[:max_tags]


def propose_description(cluster: Cluster) -> str:
    n = len(cluster.traces)
    shared = ", ".join(cluster.shared_terms[:5]) or "(no strongly shared terms -- review carefully)"
    return f"Candidate: {n} traces show a repeated pattern around: {shared}"
