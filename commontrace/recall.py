"""Multi-channel recall: one question, every kind of memory, one token budget.

Channels: approved lessons, atomic facts, knowledge-graph relations around the
entities the question names, and conversation spaces. Each channel ranks on its own;
the rankings are fused with weighted reciprocal-rank fusion, near-duplicates across
channels are suppressed (maximal marginal relevance), and the result is packed into
the budget: every channel that has something relevant gets a floor share, the rest
goes in fused order, and an item that does not fit is cut at a sentence boundary
rather than dropped when most of it fits.

`as_of` selects valid-time evidence across channels. Current lesson revocation,
forgotten/deleted sources and integrity checks still apply to historical recall;
time travel never restores trust in revoked content.

Budgets can be set per agent in `memory/budgets.json`:
    {"default": 1500, "agents": {"reviewer": {"budget": 800, "weights": {"lessons": 2}}}}"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from commontrace import paths, telemetry

CHARS_PER_TOKEN = 4
CHANNELS = ("lessons", "facts", "graph", "conversations")
DEFAULT_WEIGHTS = {"lessons": 1.0, "facts": 0.8, "graph": 0.6, "conversations": 0.9}
DEFAULT_BUDGET = 1500
FLOOR_SHARE = 0.12
RRF_K = 60
MMR_LAMBDA = 0.75
MIN_CUT_FRACTION = 0.4
_WORDS = re.compile(r"[a-z0-9]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass
class Item:
    channel: str
    id: str
    text: str
    score: float = 0.0
    at: str = ""
    fused: float = 0.0
    truncated: bool = False
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {"channel": self.channel, "id": self.id, "text": self.text, "score": round(self.score, 4),
                "fused": round(self.fused, 5), "at": self.at, "tokens": tokens(self.text),
                "truncated": self.truncated}
        if self.provenance:
            result["provenance"] = self.provenance
        return result


@dataclass(frozen=True)
class RetrievalAssessment:
    """Conservative retrieval confidence and abstention signal.

    LongMemEval treats abstention as a first-class memory ability. This signal
    describes evidence coverage only; it never claims that a generated answer
    is factually correct. Callers can refuse to answer or trigger a deeper
    reader when ``abstain`` is true.
    """

    confidence: float = 0.0
    abstain: bool = True
    reason: str = "no relevant evidence"
    channels: tuple[str, ...] = ()
    matched_query_terms: int = 0
    query_terms: int = 0

    def to_dict(self) -> dict:
        return {
            "confidence": self.confidence,
            "abstain": self.abstain,
            "reason": self.reason,
            "channels": list(self.channels),
            "matched_query_terms": self.matched_query_terms,
            "query_terms": self.query_terms,
        }


@dataclass
class Result:
    question: str
    as_of: str | None
    budget: int
    items: list[Item] = field(default_factory=list)
    considered: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    assessment: RetrievalAssessment = field(default_factory=RetrievalAssessment)
    fact_scorer: str = "overlap-v1"

    @property
    def tokens(self) -> int:
        return sum(tokens(i.text) for i in self.items)

    @property
    def context(self) -> str:
        blocks: dict[str, list[str]] = {}
        for item in self.items:
            blocks.setdefault(item.channel, []).append(item.text)
        titles = {"lessons": "Lessons", "facts": "Facts", "graph": "Relations", "conversations": "Conversations"}
        out = []
        for channel in CHANNELS:
            if channel in blocks:
                out.append(f"## {titles[channel]}\n" + "\n".join(f"- {t}" if channel != "conversations" else t
                                                                 for t in blocks[channel]))
        return "\n\n".join(out)

    def to_dict(self) -> dict:
        return {"question": self.question, "as_of": self.as_of, "budget": self.budget, "tokens": self.tokens,
                "items": [i.to_dict() for i in self.items], "considered": self.considered,
                "errors": self.errors, "assessment": self.assessment.to_dict(), "context": self.context,
                "fact_scorer": self.fact_scorer}


# --- budgets ---------------------------------------------------------------------------

def budget_config(root: str) -> dict:
    path = os.path.join(paths.memory_dir(root), "budgets.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from None
    return data if isinstance(data, dict) else {}


def resolve_budget(root: str, agent: str | None, budget: int | None,
                   weights: dict[str, float] | None) -> tuple[int, dict[str, float]]:
    cfg = budget_config(root)
    spec = ((cfg.get("agents") or {}).get(agent) or {}) if agent else {}
    total = budget or spec.get("budget") or cfg.get("default") or DEFAULT_BUDGET
    merged = {**DEFAULT_WEIGHTS, **(cfg.get("weights") or {}), **(spec.get("weights") or {}), **(weights or {})}
    return max(50, min(int(total), 200_000)), {k: float(v) for k, v in merged.items() if k in CHANNELS}


# --- channels --------------------------------------------------------------------------

def _rule(body: str) -> str:
    m = re.search(r"^## Rule\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return " ".join((m.group(1) if m else body).split())


def _lessons(root: str, question: str, as_of: str | None, k: int, scope: str = "") -> list[Item]:
    from commontrace import frontmatter, lesson_admission, lesson_cache, retrieval, retrieval_io

    active, term_cache = lesson_cache.load_active_with_terms(root, None)
    active = lesson_cache.filter_eligible(active, as_of=as_of, scope=scope)
    if not active:
        return []
    config = retrieval_io.load_config(root)
    ranked = retrieval.rank_lessons(question, active, top_k=k, floor=config.floor, scorer=config.scorer,
                                    term_cache=term_cache)
    out = []
    for r in ranked:
        try:
            fm, body = frontmatter.read(r.path)
        except Exception:  # noqa: BLE001 - an unreadable lesson is skipped, not fatal
            continue
        if not lesson_cache.fresh_eligible(r.path, fm, r.slug, as_of=as_of, root=root, body=body, scope=scope):
            continue
        text = f"{r.slug}: {fm.get('description', '')}".strip(": ")
        rule = _rule(body)
        if rule:
            text += f" Rule: {rule}"
        scopes = fm.get("scopes") or []
        traces = fm.get("source_traces") or []
        provenance = {
            "kind": "lesson",
            "scopes": [value for value in scopes if isinstance(value, str)] if isinstance(scopes, list) else [],
            "source_traces": ([value for value in traces if isinstance(value, str)][:256]
                              if isinstance(traces, list) else []),
            "admission": "verified" if fm.get(lesson_admission.RECEIPT_FIELD) else "legacy_compatible",
            "revision": lesson_admission.digest_of(fm, body),
        }
        out.append(Item("lessons", f"lesson:{r.slug}", text, r.score, provenance=provenance))
    return out


def _facts(
    root: str, question: str, as_of: str | None, k: int, evidence_budget: int = 0, scope: str = "",
    fact_scorer: str = "overlap-v1",
) -> list[Item]:
    from commontrace import fact_index, hierarchical
    from commontrace.fact_evidence import EvidenceResolver

    ranked = hierarchical.search_facts(root, question, as_of=as_of, limit=k, scope=scope, scorer=fact_scorer)
    current_facts = fact_index.snapshot_facts(root)
    resolver = EvidenceResolver(root, current_facts, as_of=as_of)
    output = []
    remaining = evidence_budget
    for fact, score in ranked:
        if score <= 0:
            continue
        current = resolver.facts.get(fact.id)
        if current is None or current.to_dict() != fact.to_dict() \
                or not resolver._fact_live(current) \
                or scope and current.scopes and scope not in current.scopes:
            continue
        provenance = {"search": {"scorer": fact_scorer,
                                  "matched_terms": fact_index.matched_terms(question, fact.statement, fact_scorer)}}
        text = fact.statement
        if fact.evidence_bound:
            # Re-check the fresh snapshot before assembling injection, including
            # a write or revocation that landed after initial candidate ranking.
            if current is None or current.revision != fact.revision or not resolver.assess(fact.id).eligible:
                continue
            provenance.update({"evidence": [receipt.to_dict() for receipt in fact.evidence],
                               "assessment": resolver.assess(fact.id).to_dict(),
                               "claim_revision": fact.evidence_revision})
            if remaining > 1:
                from commontrace.evidence_context import _explain_snapshot

                proof = _explain_snapshot(resolver, fact.id, budget=remaining - 1, scope=scope)
                if not proof.assessment.eligible or proof.claim_revision != fact.evidence_revision:
                    continue
                provenance["evidence_context"] = proof.to_dict()
                if proof.context:
                    text += "\n" + proof.context
                    remaining -= tokens(text) - tokens(fact.statement)
        output.append(Item("facts", f"fact:{fact.id}", text, score, fact.valid_from or "",
                           provenance=provenance))
    current_facts.ensure_current()
    return output


def _graph(root: str, question: str, as_of: str | None, k: int) -> list[Item]:
    from commontrace import graph

    if not os.path.exists(os.path.join(paths.memory_dir(root), "graph")) and \
            not graph.load_nodes(root):
        return []
    starts = [e for e in graph.extract_entities_from_text(root, question) if not e.startswith("lesson:")]
    if not starts:
        return []
    sub = graph.multi_hop_subgraph(root, start_node_ids=starts, max_hops=1, as_of=as_of, max_edges=k * 4)
    hops: dict[str, int] = sub.get("hop_distances", {})
    out: list[Item] = []
    for edge in sub.get("edges", []):
        src, dst, rel = edge.get("source"), edge.get("target"), edge.get("relation")
        if str(src).startswith("lesson:") or str(dst).startswith("lesson:"):
            continue
        since = (edge.get("valid_at") or "")[:10]
        text = f"{src} {str(rel).replace('_', ' ')} {dst}" + (f" (since {since})" if since else "")
        score = 1.0 / (1 + min(hops.get(src, 1), hops.get(dst, 1))) * float(edge.get("weight", 1.0) or 1.0)
        out.append(Item("graph", f"edge:{src}->{dst}:{rel}", text, score, edge.get("valid_at") or ""))
    return sorted(out, key=lambda i: -i.score)[:k]


def _conversations(root: str, question: str, as_of: str | None, budget: int, spaces: list[str] | None,
                   embedder: str) -> list[Item]:
    from commontrace import conversation

    names = spaces if spaces is not None else conversation.spaces(root)
    out: list[Item] = []
    for space in names:
        with conversation.Store(root, space, create=False) as store:
            opts = conversation.Options(budget=max(100, budget), until=as_of,
                                        embedder=None if embedder in ("", "none") else embedder)
            result = conversation.recall(store, question, now=as_of, options=opts)
        if result.context.strip():
            out.append(Item("conversations", f"space:{space}", f"[{space}]\n{result.context}",
                            1.0 / (1 + len(out)), provenance={"coverage": result.explain.get("coverage", {})}))
    return out


# --- fusion, diversity, packing -----------------------------------------------------------

def fuse(rankings: dict[str, list[Item]], weights: dict[str, float]) -> list[Item]:
    """Weighted reciprocal-rank fusion across channels."""
    pool = []
    for channel, items in rankings.items():
        w = weights.get(channel, 1.0)
        for rank, item in enumerate(items):
            item.fused = w / (RRF_K + rank + 1)
            pool.append(item)
    return sorted(pool, key=lambda i: -i.fused)


def _terms(text: str) -> set[str]:
    return {w for w in _WORDS.findall(text.lower()) if len(w) > 2}


def diversify(items: list[Item], lam: float = MMR_LAMBDA) -> list[Item]:
    """Maximal marginal relevance: each next item trades fused relevance against its
    word overlap with what is already chosen; exact restatements are dropped."""
    if not items:
        return []
    top = items[0].fused or 1.0
    terms = [_terms(i.text) for i in items]
    chosen: list[int] = []
    left = list(range(len(items)))
    while left:
        best, best_score = None, None
        for i in left:
            overlap = max((len(terms[i] & terms[j]) / (len(terms[i] | terms[j]) or 1) for j in chosen), default=0.0)
            if overlap >= 0.9:
                continue
            score = lam * (items[i].fused / top) - (1 - lam) * overlap
            if best_score is None or score > best_score:
                best, best_score = i, score
        if best is None:
            break
        chosen.append(best)
        left.remove(best)
    return [items[i] for i in chosen]


def truncate(text: str, max_tokens: int) -> str:
    """Cut at the last sentence (or word) boundary that fits."""
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentences = _SENTENCE.split(cut)
    if len(sentences) > 1:
        kept = cut[:len(cut) - len(sentences[-1])].rstrip()
        if len(kept) >= limit * 0.5:
            return kept + " ..."
    return cut.rsplit(" ", 1)[0].rstrip() + " ..."


def pack(items: list[Item], budget: int) -> list[Item]:
    """Fill the budget: a floor share first for each channel's best item, then fused
    order; an item that does not fit is cut when at least MIN_CUT_FRACTION of it (or a
    quarter of the budget) fits."""
    chosen: list[Item] = []
    used = 0
    channels = list(dict.fromkeys(i.channel for i in items))
    floor = int(budget * FLOOR_SHARE)
    seen: set[str] = set()

    def take(item: Item, room: int) -> None:
        nonlocal used
        need = tokens(item.text)
        if need <= room:
            chosen.append(item)
            used += need
        elif room >= max(20, min(int(need * MIN_CUT_FRACTION), budget // 4)):
            cut = Item(item.channel, item.id, truncate(item.text, room), item.score, item.at, item.fused, True,
                       provenance=item.provenance)
            chosen.append(cut)
            used += tokens(cut.text)
        seen.add(item.id)

    for n, channel in enumerate(channels):
        first = next(i for i in items if i.channel == channel)
        reserved = floor * (len(channels) - n - 1)  # keep a floor for every channel still to come
        take(first, max(0, min(budget - used - reserved, max(floor, tokens(first.text)))))
    for item in items:
        if item.id in seen or used >= budget:
            continue
        take(item, budget - used)
    order = {i.id: n for n, i in enumerate(items)}
    return sorted(chosen, key=lambda i: order[i.id])


def _assess_retrieval(
    question: str,
    items: list[Item],
    errors: dict,
) -> RetrievalAssessment:
    """Compute a deterministic evidence coverage signal.

    It combines lexical query coverage, strongest channel score, and channel
    diversity. The result is intentionally conservative: it is a retrieval
    gate for abstention/deeper reading, not an answer truth score.
    """
    from commontrace.conversation.coverage import assess

    evidence = assess(question, [item.text for item in items])
    if not items:
        return RetrievalAssessment(reason="no channel returned relevant evidence", query_terms=evidence.query_terms)
    matched = evidence.matched_terms
    coverage = evidence.confidence
    strongest = min(1.0, max(0.0, max((float(item.score) for item in items), default=0.0)))
    channels = tuple(sorted({item.channel for item in items}))
    if evidence.abstain:
        return RetrievalAssessment(reason=evidence.reason, channels=channels,
                                   matched_query_terms=matched, query_terms=evidence.query_terms)
    diversity = min(1.0, len(channels) / 3.0)
    confidence_raw = 0.55 * strongest + 0.30 * coverage + 0.15 * diversity
    confidence = round(min(1.0, confidence_raw * (0.8 if errors else 1.0)), 4)
    if not evidence.query_terms or matched == 0:
        reason = "retrieved items do not cover the query terms"
    elif confidence < 0.2:
        reason = "evidence is weak; use a deeper reader or ask for clarification"
    elif errors:
        reason = "some memory channels failed; confidence is intentionally reduced"
    else:
        reason = "retrieved evidence covers the query"
    return RetrievalAssessment(
        confidence=confidence,
        abstain=confidence < 0.2 or matched == 0,
        reason=reason,
        channels=channels,
        matched_query_terms=matched,
        query_terms=evidence.query_terms,
    )


def recall(root: str, question: str, *, budget: int | None = None, agent: str | None = None,
           channels: tuple[str, ...] = CHANNELS, as_of: str | None = None, weights: dict[str, float] | None = None,
           spaces: list[str] | None = None, embedder: str = "none", per_channel: int = 12,
           evidence_budget: int = 0, scope: str = "", fact_scorer: str = "overlap-v1") -> Result:
    """Recall across channels; opt into bounded fact source quotes.

    Nonempty ``scope`` restricts lessons/facts to that scope or public memory.
    Graph reads are withheld until the graph supports scoped authorization.
    Conversation spaces must be explicitly selected by the authorized caller
    for scoped reads; no scoped request enumerates the root's other spaces.
    Empty scope preserves trusted-local root-wide behavior.
    """
    from commontrace import lesson_cache

    if isinstance(evidence_budget, bool) or not isinstance(evidence_budget, int) or not 0 <= evidence_budget <= 8192:
        raise ValueError("evidence_budget must be an integer between 0 and 8192")
    if not isinstance(scope, str) or len(scope) > 256 or any(ord(char) < 32 for char in scope):
        raise ValueError("scope must be a bounded string without control characters")
    if not isinstance(fact_scorer, str) or fact_scorer not in ("overlap-v1", "bm25-v1"):
        raise ValueError("fact_scorer must be overlap-v1 or bm25-v1")

    question = (question or "").strip()
    if as_of:
        lesson_cache.parse_moment(as_of)  # refuse a bad moment before reading anything
    total, weights = resolve_budget(root, agent, budget, weights)
    result = Result(question, as_of, total, fact_scorer=fact_scorer)
    if not question:
        return result
    bad = [c for c in channels if c not in CHANNELS]
    if bad:
        raise ValueError(f"unknown channel(s): {', '.join(bad)}; choose from {', '.join(CHANNELS)}")
    rankings: dict[str, list[Item]] = {}
    with telemetry.span("recall.multi", channels=",".join(channels), budget=total) as handle:
        for channel in channels:
            try:
                with telemetry.span(f"recall.{channel}"):
                    if channel == "lessons":
                        found = _lessons(root, question, as_of, per_channel, scope)
                    elif channel == "facts":
                        found = _facts(root, question, as_of, per_channel, evidence_budget, scope, fact_scorer)
                    elif channel == "graph":
                        found = _graph(root, question, as_of, per_channel) if not scope else []
                    else:
                        found = (_conversations(root, question, as_of, int(total * 0.6), spaces, embedder)
                                 if not scope or spaces is not None else [])
            except Exception as exc:  # noqa: BLE001 - one broken channel does not sink the others
                result.errors[channel] = f"{type(exc).__name__}: {exc}"
                continue
            result.considered[channel] = len(found)
            if found:
                rankings[channel] = found
        result.items = pack(diversify(fuse(rankings, weights)), total)
        result.assessment = _assess_retrieval(question, result.items, result.errors)
        handle.set(tokens=result.tokens, items=len(result.items), confidence=result.assessment.confidence)
    telemetry.observe("commontrace_recall_tokens", float(result.tokens))
    return result
