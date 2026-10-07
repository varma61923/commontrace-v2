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
    created_at: str


@dataclass
class Signal:
    name: str
    trace_ids: list[str] = field(default_factory=list)
    shared_terms: list[str] = field(default_factory=list)
    affected_agents: list[str] = field(default_factory=list)
    trend: str = "unknown"
    first_seen: str = ""
    last_seen: str = ""

    @property
    def size(self) -> int:
        return len(self.trace_ids)


def _is_failure(outcome: dict[str, object] | None) -> bool:
    if not isinstance(outcome, dict):
        return False
    if outcome.get("resolved") is False:
        return True
    return outcome.get("repeated_error") is True


def _safe_tags(raw: object) -> list[str]:
    return [str(t) for t in raw if t is not None] if isinstance(raw, (list, tuple)) else []


def load_failure_occurrences(root: str, agent_type: str | None = None) -> list[FailureOccurrence]:
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


_MIN_DATED_PER_HALF = 2
_TREND_RATIO = 1.3


def _parse_iso(ts: str) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _trend(dated: list[str]) -> str:
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


DEFAULT_SIMILARITY = 0.15


def _vectors(occurrences: list[FailureOccurrence]) -> list[dict[str, float]]:
    import math
    from collections import Counter

    from commontrace import _stem
    from commontrace._lexical import STOPWORDS, WORD_RE

    def tokens(text: str) -> list[str]:
        return [_stem.stem(w) for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]

    docs = [Counter(tokens(f"{o.title} {o.context_text}")) for o in occurrences]
    df: Counter[str] = Counter(t for d in docs for t in d)
    n = len(docs)
    out = []
    for d in docs:
        v = {t: (1 + math.log(c)) * math.log((n + 1) / (df[t] + 0.5)) for t, c in d.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        out.append({t: x / norm for t, x in v.items()})
    return out


def _average_linkage(vectors: list[dict[str, float]], threshold: float) -> list[list[int]]:
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
            continue
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
    """Cluster this store's failing traces into named signals."""
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


def export_langsmith(signal: Signal, by_id: dict[str, FailureOccurrence]) -> list[dict[str, object]]:
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


def export_braintrust(signal: Signal, by_id: dict[str, FailureOccurrence]) -> list[dict[str, object]]:
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
