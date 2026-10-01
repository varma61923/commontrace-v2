"""Failure signals: named, sized, trended clusters of FAILING occasions,
with an export path to a regression dataset a customer's existing eval
tool (LangSmith, Braintrust) can run against.

Reuses `commontrace/distill.py`'s clustering -- the same word-overlap
Jaccard grouping `commontrace distill`/`commontrace taxonomy` already use
-- restricted to traces this store recorded as a FAILURE
(`outcome.resolved is False`, or `outcome.repeated_error is True` when
`resolved` itself is unset). `distill`/`taxonomy` deliberately do not
filter this way: they report on every repeated pattern, not specifically
ones that failed, and a second clustering algorithm here would be exactly
the kind of parallel implementation this codebase's own culture (see
distill.py's own module docstring) refuses to add.

A "signal" adds two things a candidate lesson does not need and a
regression dataset does: WHEN (is this getting more common, or did it
stop) and WHO (which agents hit it), both read from fields every Trace
already carries (`created_at`, `agent_id`) that `distill.TraceCandidate`
does not -- so this module reads traces directly rather than through that
narrower shape.

EXPORT FORMATS ARE NOT GUESSED
--------------------------------
`export_langsmith`/`export_braintrust` use the SAME field names
`commontrace/adapters.py`'s `_langsmith`/`_braintrust` importers already
read back OUT of a real export from each vendor (`inputs`/`outputs`
for LangSmith, `input`/`expected` for Braintrust) -- used here in reverse,
rather than a second, independently-guessed vocabulary for the same two
vendors' shapes.
"""
from __future__ import annotations

import datetime
import glob
import os
from dataclasses import dataclass, field

from commontrace import distill, paths, trace_io
from commontrace.commands._format import read_or_warn


@dataclass(frozen=True)
class FailureOccurrence:
    id: str
    title: str
    context_text: str
    solution_text: str
    tags: list[str]
    agent_type: str
    agent_id: str
    #: ISO-8601 from Trace.created_at, or "" if the trace predates it /
    #: was hand-written without one. Never guessed.
    created_at: str


@dataclass
class Signal:
    name: str
    trace_ids: list[str] = field(default_factory=list)
    shared_terms: list[str] = field(default_factory=list)
    #: Distinct, non-empty agent_id values among this signal's occurrences,
    #: sorted for a stable report. Empty when no occurrence recorded one --
    #: never populated from agent_type, which is a category, not an agent.
    affected_agents: list[str] = field(default_factory=list)
    #: "increasing" | "steady" | "decreasing" | "unknown". See `_trend`'s
    #: own docstring for exactly what decides each.
    trend: str = "unknown"
    first_seen: str = ""
    last_seen: str = ""

    @property
    def size(self) -> int:
        return len(self.trace_ids)


def _is_failure(outcome: dict | None) -> bool:
    """A trace counts as a failure signal candidate when it says so
    explicitly. A trace with NO outcome recorded is not a failure by
    default -- "no signal" and "failed" are different facts, and treating
    an unrecorded outcome as a failure would silently inflate every
    signal's size with traces that may have gone fine."""
    if not isinstance(outcome, dict):
        return False
    if outcome.get("resolved") is False:
        return True
    return outcome.get("repeated_error") is True


def _safe_tags(raw: object) -> list[str]:
    return [str(t) for t in raw if t is not None] if isinstance(raw, (list, tuple)) else []


def load_failure_occurrences(root: str, agent_type: str | None = None) -> list[FailureOccurrence]:
    """Every trace in this store recorded as a failure, optionally scoped
    to one `agent_type`."""
    out: list[FailureOccurrence] = []
    for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        parsed = read_or_warn(trace_io.read, path)
        if parsed is None:
            continue
        inst, _body = parsed
        if not _is_failure(inst.get("outcome")):
            continue
        if agent_type and inst.get("agent_type") != agent_type:
            continue
        trace_id = str(inst.get("id") or "")
        if not trace_id:
            continue
        out.append(FailureOccurrence(
            id=trace_id,
            title=str(inst.get("title") or ""),
            context_text=str(inst.get("context_text") or ""),
            solution_text=str(inst.get("solution_text") or ""),
            tags=_safe_tags(inst.get("tags")),
            agent_type=str(inst.get("agent_type") or ""),
            agent_id=str(inst.get("agent_id") or ""),
            created_at=str(inst.get("created_at") or ""),
        ))
    return out


#: A signal needs at least this many dated occurrences on EACH side of the
#: chronological split before trend is anything other than "unknown" --
#: below it, a 1-vs-0 split would report "increasing" from noise.
_MIN_DATED_PER_HALF = 2
#: How much the second half has to exceed (or fall short of) the first to
#: be called a trend rather than "steady" -- a >=30% swing either way,
#: chosen the same way commontrace/reliability.py picks a boundary rather
#: than reporting every fluctuation as a trend.
_TREND_RATIO = 1.3


def _parse_iso(ts: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _trend(dated: list[str]) -> str:
    """`dated` is this signal's occurrences' `created_at` values, in no
    particular order coming in.

    Splits at the TIME midpoint between the earliest and latest occurrence
    -- not at the rank/count midpoint. Splitting by count instead (an
    earlier version of this function did) makes each half's size roughly
    equal BY CONSTRUCTION regardless of how bunched the actual dates are,
    which means a genuine frequency change can never show up as a count
    difference at all; reproduced directly, two occurrences in January and
    seven bunched in February read as "steady" under a count-based split
    because the median landed inside February. Splitting by elapsed time
    instead makes a density change visible as a count difference on each
    side, which is the thing "trend" is actually supposed to mean.
    """
    parsed = sorted(d for d in (_parse_iso(t) for t in dated) if d is not None)
    if len(parsed) < _MIN_DATED_PER_HALF * 2:
        return "unknown"
    midpoint = parsed[0] + (parsed[-1] - parsed[0]) / 2
    first_half = [d for d in parsed if d <= midpoint]
    second_half = [d for d in parsed if d > midpoint]
    if len(first_half) < _MIN_DATED_PER_HALF or len(second_half) < _MIN_DATED_PER_HALF:
        return "unknown"
    if len(second_half) >= len(first_half) * _TREND_RATIO:
        return "increasing"
    if len(second_half) <= len(first_half) / _TREND_RATIO:
        return "decreasing"
    return "steady"


#: Cosine similarity of stemmed, IDF-weighted title+context vectors at which two groups of failures
#: are one signal (average linkage). Chosen on development seeds 100-104 of commons/eval/signal_ari.py
#: and reported on held-out seeds 0-19; a different scale from `distill`'s word-overlap threshold.
DEFAULT_SIMILARITY = 0.15


def _vectors(occurrences: list[FailureOccurrence]) -> list[dict[str, float]]:
    """Unit-length TF-IDF vectors of each failure's title and context, stemmed.

    IDF is computed over THESE failures, so boilerplate that appears across all of them ("the customer
    was frustrated") carries almost no weight and what distinguishes one failure mode from another
    carries most. The solution text is not used: it is what was done about it, and a signal is meant to
    group by what went wrong, including failures nobody has yet fixed."""
    import math
    from collections import Counter

    from commontrace import _stem
    from commontrace._lexical import STOPWORDS, WORD_RE

    def tokens(text: str) -> list[str]:
        return [_stem.stem(w) for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]

    docs = [Counter(tokens(f"{o.title} {o.context_text}")) for o in occurrences]
    df: Counter = Counter(t for d in docs for t in d)
    n = len(docs)
    out = []
    for d in docs:
        v = {t: (1 + math.log(c)) * math.log((n + 1) / (df[t] + 0.5)) for t, c in d.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        out.append({t: x / norm for t, x in v.items()})
    return out


def _average_linkage(vectors: list[dict[str, float]], threshold: float) -> list[list[int]]:
    """Agglomerative clustering, average linkage (UPGMA), merging while the best pair's similarity is at
    least `threshold`. Sparse: only pairs that share a term have a similarity, the rest are 0 and can
    never merge at a positive threshold, so cost follows the number of overlapping pairs, not n squared
    over everything. Average linkage, not single: one shared sentence cannot chain two different
    failure modes into one."""
    import heapq

    n = len(vectors)
    index: dict[str, list[int]] = {}
    for i, v in enumerate(vectors):
        for t in v:
            index.setdefault(t, []).append(i)
    sims: dict[int, dict[int, float]] = {i: {} for i in range(n)}
    for i, v in enumerate(vectors):
        scores: dict[int, float] = {}
        for t, x in v.items():
            for j in index[t]:
                if j > i:
                    scores[j] = scores.get(j, 0.0) + x * vectors[j][t]
        for j, sim in scores.items():
            if sim > 0:
                sims[i][j] = sims[j][i] = sim
    members = {i: [i] for i in range(n)}
    heap = [(-sim, i, j) for i in range(n) for j, sim in sims[i].items() if j > i]
    heapq.heapify(heap)
    next_id = n
    while heap:
        neg, a, b = heapq.heappop(heap)
        if -neg < threshold:
            break
        if a not in members or b not in members or sims[a].get(b) != -neg:
            continue  # stale: one side was merged since this entry was pushed
        na, nb = len(members[a]), len(members[b])
        c = next_id
        next_id += 1
        merged: dict[int, float] = {}
        for other in (sims[a].keys() | sims[b].keys()) - {a, b}:
            merged[other] = (na * sims[a].get(other, 0.0) + nb * sims[b].get(other, 0.0)) / (na + nb)
        for x in (a, b):
            for other in sims.pop(x):
                if other in sims:
                    sims[other].pop(x, None)
        sims[c] = merged
        for other, sim in merged.items():
            sims[other][c] = sim
            if sim >= threshold:
                heapq.heappush(heap, (-sim, min(c, other), max(c, other)))
        members[c] = members.pop(a) + members.pop(b)
    return list(members.values())


def build_signals(
    root: str, *, agent_type: str | None = None,
    similarity_threshold: float = DEFAULT_SIMILARITY, min_cluster_size: int = 2,
) -> tuple[list[Signal], dict[str, FailureOccurrence]]:
    """Cluster this store's failing traces into named signals.

    Returns `(signals, by_id)` -- `by_id` is every occurrence clustered
    into any returned signal, keyed by trace id, so a caller (the CLI
    command, `export_langsmith`/`export_braintrust`) can look up the full
    record for a signal's `trace_ids` without a second read of the store.
    """
    occurrences = load_failure_occurrences(root, agent_type)
    by_id = {occ.id: occ for occ in occurrences}
    groups = _average_linkage(_vectors(occurrences), similarity_threshold) if similarity_threshold > 0 else \
        [list(range(len(occurrences)))] if occurrences else []
    clusters = []
    for group in groups:
        if len(group) < min_cluster_size:
            continue
        traces = [distill.TraceCandidate(
            id=occurrences[i].id, path="", title=occurrences[i].title, context_text=occurrences[i].context_text,
            solution_text=occurrences[i].solution_text, tags=occurrences[i].tags,
            agent_type=occurrences[i].agent_type) for i in group]
        shared = set.intersection(*(distill._tokenize(f"{t.title} {t.context_text}") for t in traces))
        clusters.append(distill.Cluster(traces=traces, shared_terms=sorted(shared)[:8]))
    clusters.sort(key=lambda c: len(c.traces), reverse=True)

    signals: list[Signal] = []
    for cluster in clusters:
        members = [by_id[t.id] for t in cluster.traces]
        dated = sorted(m.created_at for m in members if m.created_at)
        agents = sorted({m.agent_id for m in members if m.agent_id})
        signals.append(Signal(
            name=distill.propose_description(cluster),
            trace_ids=[m.id for m in members],
            shared_terms=cluster.shared_terms,
            affected_agents=agents,
            trend=_trend(dated),
            first_seen=dated[0] if dated else "",
            last_seen=dated[-1] if dated else "",
        ))
    return signals, by_id


def export_langsmith(signal: Signal, by_id: dict[str, FailureOccurrence]) -> list[dict]:
    """One example per occurrence, in LangSmith's bulk-example shape --
    the same `inputs`/`outputs` field names `commontrace/adapters.py`'s
    `_langsmith` importer already reads back OUT of a real LangSmith
    export, used here in reverse."""
    return [
        {
            "inputs": {"input": occ.context_text},
            "outputs": {"output": occ.solution_text},
            "metadata": {
                "commontrace_signal": signal.name,
                "commontrace_trace_id": occ.id,
                "tags": occ.tags,
            },
        }
        for occ in (by_id[i] for i in signal.trace_ids if i in by_id)
    ]


def export_braintrust(signal: Signal, by_id: dict[str, FailureOccurrence]) -> list[dict]:
    """One record per occurrence, in Braintrust's bulk dataset-insert shape
    -- the same `input`/`expected` field names `_braintrust` already reads
    back OUT of a real Braintrust export."""
    return [
        {
            "input": occ.context_text,
            "expected": occ.solution_text,
            "metadata": {
                "commontrace_signal": signal.name,
                "commontrace_trace_id": occ.id,
                "tags": occ.tags,
            },
        }
        for occ in (by_id[i] for i in signal.trace_ids if i in by_id)
    ]
