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
from collections import Counter
from dataclasses import dataclass, field

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace.paths import STARTER_DOMAINS

# Unicode-aware -- see commontrace/retrieval.py for why the ASCII-only class
# was wrong. Clustering non-English traces produced 0.0 Jaccard for every
# pair, so `distill` reported "no repeated patterns" on a corpus full of
# them. Shared with retrieval.py via commontrace/_lexical.py rather than a
# second local copy -- these two had already drifted from each other once.


_DOMAIN_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _domain_slug(value: str) -> str:
    """A tag or shared term, reduced to something usable as a `domain`.

    lesson.schema.json leaves `domain` an open string, but it is compared
    for equality across lessons (taxonomy coverage, retrieval's domain
    field), so a label that differs only by case or punctuation would split
    one domain into several. Returns "" for anything that survives as empty
    or as a pure number, which the caller reads as "try the next candidate".
    """
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
    """A domain label for this candidate, from the cluster's own vocabulary.

    STARTER_DOMAINS only has starter vocabularies for the handful of fleets
    that shipped with one. The taxonomy itself is open
    (protocol/PROTOCOL.md#7), so a robotics or legal fleet has no starter
    list -- and the previous fallback chain (`STARTER_DOMAINS["custom"]`,
    then `starter[-1]`) labelled EVERY candidate that fleet ever produced
    `other`, collapsing a whole field's taxonomy into one bucket and making
    `commontrace taxonomy`'s coverage report meaningless for it.

    The same fallback misfired for listed fleets too: an unmatched `support`
    cluster was labelled `known-issues` purely because that happens to be the
    last entry in support's starter list.

    So: prefer a starter domain when one is actually present in the cluster's
    tags (unchanged), then fall back to the cluster's OWN most common tag --
    curated vocabulary, and a far better label than `other` -- then to its
    strongest shared term, and only then to `other`.
    """
    starter = STARTER_DOMAINS.get(agent_type, [])
    cluster_tags = [t for trace in cluster.traces for t in trace.tags]
    tag_set = set(cluster_tags)
    for domain in starter:
        if domain in tag_set:
            return domain

    # Counter preserves first-seen order on ties, so this is deterministic.
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

    "Best" is the one whose title+context words overlap most with the rest of
    the cluster, so it is the most typical member rather than the first one
    the filesystem happened to return. Ties break on trace id, because a
    proposal that changes text between two identical runs is a proposal
    nobody can review.
    """
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
    """Distinct versions of a repeated field, most common first, with counts.

    A cluster is by construction a set of SIMILAR traces, so listing all of
    them verbatim prints the same paragraph a dozen times -- which is what a
    proposed lesson used to do, and it made the evidence section noise
    rather than evidence. Grouping collapses that, and the counts turn it
    into information the reviewer needs.

    The interesting case is when there is more than one variant of what
    WORKED. That means the same symptom had different causes, and it is
    exactly the signal that a candidate should be split or rejected rather
    than written up as one rule -- so the variants are shown rather than the
    most common one alone.

    Grouped on normalized text (case, whitespace, trailing punctuation) so
    two copies differing by a stray space count as one; anything differing
    by a word is a genuine variant and stays separate.
    """
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
    """A sentence a human can read, not a bag of words.

    This used to be `"Candidate: 12 traces show a repeated pattern around:
    anywhere, byte, csv, customer, empty"` -- the cluster's shared TERMS,
    which after stopword removal are whatever survived, in no particular
    order. Two things were wrong with that, and both cost more than they
    look:

    1. It is what a curator reads in `commontrace lesson list`, so a store
       with a dozen candidates was a dozen indistinguishable term lists and
       the review queue did not get worked.
    2. `description` is a ranked retrieval field (commontrace/retrieval.py),
       so those tokens -- "anywhere", "byte", "customer" -- were what the
       candidate matched on.

    The medoid trace's own title is already a sentence written by a person
    about this exact failure. Using it costs nothing and is strictly more
    informative. It stays marked as a proposal, because it IS one: one real
    example standing in for a cluster, which a reviewer should generalise.
    """
    n = len(cluster.traces)
    title = (representative(cluster).title or "").strip()
    if not title:
        shared = ", ".join(cluster.shared_terms[:5]) or "(no strongly shared terms)"
        return f"Candidate ({n} traces): repeated pattern around {shared}"
    return f"{title} — and {n - 1} more like it" if n > 1 else title
