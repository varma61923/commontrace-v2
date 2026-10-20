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
    {"default": 1500, "agents": {"reviewer": {"budget": 800, "weights": {"lessons": 2}}}}

A second-stage reranker (a local cross-encoder, Cohere, Voyage, Jina or an LLM;
see `commontrace.reranking`) can rerank the head of the fused ranking before
diversity and packing, per call or in the same file, globally or per agent:
    {"rerank": {"reranker": "cohere:rerank-v3.5", "depth": 30, "blend": 0.5},
     "adaptive_budget": true, "max_budget": 6000}
`adaptive_budget` sizes the budget by the question's shape and grows it while
retrieved evidence stays weak. Both are off unless asked for; every result
says what ran in ``explain``, with a lexical completeness grade."""
from __future__ import annotations

import json
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from commontrace import paths, telemetry
from commontrace.configuration import getenv as _configuration_getenv

CHARS_PER_TOKEN = 4
CHANNELS = ("lessons", "facts", "graph", "conversations")
DEFAULT_WEIGHTS = {"lessons": 1.0, "facts": 0.8, "graph": 0.6, "conversations": 0.9}
DEFAULT_BUDGET = 1500
FLOOR_SHARE = 0.12
RRF_K = 60
# A dense-only fact must stay within DENSE_BAND of the best cosine and, once the pool is large enough
# to say what "ordinary" looks like, stand DENSE_OUTLIER_SIGMA robust deviations above its median.
# Cosine baselines differ by model (about 0.1 for arctic, 0.45 for bge-small), so no fixed cutoff
# is right; a floor relative to half the best score admitted every fact for bge-small.
DENSE_BAND = 0.12
DENSE_OUTLIER_SIGMA = 4.0
DENSE_MIN_POOL = 8
DENSE_MIN_SPREAD = 0.02
DENSE_ONLY_SCALE = 0.5  # a fact found only by embedding scores half its cosine (see _hybrid)
MMR_LAMBDA = 0.75
MIN_CUT_FRACTION = 0.4
ADAPTIVE_CAP = 12_000  # the default ceiling an adaptive budget may grow to
LOW_CONFIDENCE = 0.35  # below this, an adaptive budget grows while candidates remain unpacked
MAX_GROWTH_STEPS = 2  # each step doubles the budget, up to the cap
MAX_PER_CHANNEL = 64
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
    explain: dict = field(default_factory=dict)

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
                "fact_scorer": self.fact_scorer, "explain": self.explain}


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


def retrieval_settings(root: str, agent: str | None = None) -> dict:
    """Reranker and adaptive-budget settings from `memory/budgets.json`, agent over global.

    Returns ``{"reranker", "depth", "blend", "adaptive_budget", "max_budget"}``;
    ``"rerank"`` in the file may be a reranker name or an object with those keys.
    """
    from commontrace import reranking

    cfg = budget_config(root)
    spec = ((cfg.get("agents") or {}).get(agent) or {}) if agent else {}
    out = {"reranker": None, "depth": reranking.DEFAULT_DEPTH, "blend": reranking.DEFAULT_BLEND,
           "adaptive_budget": False, "max_budget": None}
    for layer in (cfg, spec):
        if not isinstance(layer, dict):
            continue
        rerank = layer.get("rerank")
        if isinstance(rerank, str):
            rerank = {"reranker": rerank}
        if isinstance(rerank, dict):
            out.update({key: rerank[key] for key in ("reranker", "depth", "blend") if key in rerank})
        out.update({key: layer[key] for key in ("adaptive_budget", "max_budget") if key in layer})
    if not isinstance(out["adaptive_budget"], bool):
        raise ValueError("budgets.json: adaptive_budget must be true or false")
    if out["max_budget"] is not None and (isinstance(out["max_budget"], bool)
                                          or not isinstance(out["max_budget"], int)):
        raise ValueError("budgets.json: max_budget must be an integer")
    if out["reranker"] is not None and not isinstance(out["reranker"], str):
        raise ValueError("budgets.json: rerank.reranker must be a reranker name")
    reranking.check_options(out["depth"], out["blend"])
    return out


# --- channels --------------------------------------------------------------------------

def _rule(body: str) -> str:
    m = re.search(r"^## Rule\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return " ".join((m.group(1) if m else body).split())


def _memory_id(item_id: str) -> str:
    """The governed memory id behind a recall item id (`lesson:slug`, `fact:id`)."""
    kind, sep, rest = item_id.partition(":")
    return rest if sep and kind in ("lesson", "fact") else item_id


def _lessons(root: str, question: str, as_of: str | None, k: int, scope: str = "") -> list[Item]:
    from commontrace import frontmatter, lesson_admission, lesson_cache, retrieval, retrieval_io

    active, term_cache = lesson_cache.load_active_with_terms(root, None)
    active = lesson_cache.filter_eligible(active, as_of=as_of, scope=scope)
    if not active:
        return []
    config = retrieval_io.load_config(root)
    ranked = retrieval.rank_lessons(question, active, top_k=k, floor=config.floor, scorer=config.scorer,
                                    term_cache=term_cache)
    # The label a lexical retrieve logs with its holdout rows (harm-budget scale).
    label = retrieval_io.rerank_label(config.eligibility_label_for(fused=False), retrieval_io.RERANK_NONE)
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
        provenance["relevance"] = r.relevance
        provenance["scorer"] = label
        out.append(Item("lessons", f"lesson:{r.slug}", text, r.score, provenance=provenance))
    return out


def _dense_select(scored: list, k: int) -> dict:
    """{fact id: (fact, cosine)} for the dense hits that stand out from the pool, best first.

    Cosines are not comparable across embedding models, so relevance is judged against the pool
    itself: within DENSE_BAND of the best match and, for pools of DENSE_MIN_POOL or more, above the
    pool median by DENSE_OUTLIER_SIGMA robust deviations. When nothing stands out (the best score is
    just the largest of a noisy pool) nothing is returned, so an unrelated question injects no facts.
    """
    ranked = sorted(scored, key=lambda pair: (-pair[1], pair[0].id))
    if not ranked or ranked[0][1] <= 0:
        return {}
    values = [cosine for _fact, cosine in ranked]
    floor = values[0] - DENSE_BAND
    if len(values) >= DENSE_MIN_POOL:
        median = statistics.median(values)
        spread = 1.4826 * statistics.median(abs(value - median) for value in values)
        floor = max(floor, median + DENSE_OUTLIER_SIGMA * max(spread, DENSE_MIN_SPREAD))
    return {fact.id: (fact, cosine) for fact, cosine in ranked[:k] if cosine > 0 and cosine >= floor}


def _dense_facts(root: str, question: str, as_of: str | None, k: int, scope: str) -> dict:
    """{fact id: (fact, cosine)} for the `k` facts nearest the question, or {} without a fact embedder.

    Use a completed Lance fact projection when available; falling back to exact
    cached-vector scoring preserves the small-store and legacy-store behavior.
    """
    from commontrace import fact_embeddings, hierarchical

    model = _configuration_getenv("COMMONTRACE_FACT_EMBEDDER", "").strip()
    model_path = _configuration_getenv("COMMONTRACE_FACT_EMBEDDER_PATH")
    if model.lower() in ("none", "off"):
        return {}
    if not model and not model_path:
        from commontrace import embeddings

        model = embeddings.DEFAULT_LOCAL_TAG
    if not (model or model_path):
        return {}

    # A configured Lance projection avoids enumerating and embedding the full
    # million-fact corpus on every aggregate recall. Keep the exact path below
    # for stores and configurations that do not have an indexed projection.
    from commontrace import fact_store, fact_vector_projection

    if model and not model_path and not as_of and fact_store.enabled(root) \
            and fact_vector_projection._backend(root) == "lance":
        watermark = fact_vector_projection._watermark(model, "lance")
        moment = datetime.now(timezone.utc)
        with fact_store.transaction(root) as current:
            row = current.connection.execute("SELECT value FROM meta WHERE key=?", (watermark,)).fetchone()
            try:
                cursor = int(row[0]) if row else 0
                indexed = row is not None
            except (TypeError, ValueError):
                cursor, indexed = 0, False
            pending_ids = list(dict.fromkeys(row[0] for row in current.connection.execute(
                "SELECT fact_id FROM events WHERE seq>? ORDER BY seq", (cursor,)
            ))) if indexed else []
            pending_facts = []
            for identity in pending_ids:
                fact = current.get(identity)
                if fact is not None and not fact.forgotten and fact.status == "active" \
                        and (not scope or not fact.scopes or scope in fact.scopes) \
                        and hierarchical._valid_at(fact, moment) and not hierarchical._is_expired(fact, moment):
                    pending_facts.append(fact)
        if indexed:
            try:
                candidates = fact_vector_projection.search(root, question, model=model,
                                                           limit=max(200, min(1000, k * 16)))
                # A stale projection can omit newly added or revised facts. Score
                # just that bounded outbox tail exactly instead of rescanning the
                # full canonical store, then merge it with indexed candidates.
                pending_scores = fact_embeddings.scores(
                    question, pending_facts, model=model, root=root
                ) or {}
                candidate_scores = dict(candidates)
                candidate_scores.update(pending_scores)
                from commontrace import memory_authority
                from commontrace.fact_evidence import EvidenceResolver

                with fact_store.transaction(root) as current:
                    resolver = EvidenceResolver(root, current)
                    ranked = []
                    for identity, cosine in candidate_scores.items():
                        fact = current.get(identity)
                        if fact is None or fact.forgotten or fact.status != "active" \
                                or scope and fact.scopes and scope not in fact.scopes \
                                or not hierarchical._valid_at(fact, moment) \
                                or hierarchical._is_expired(fact, moment) \
                                or memory_authority.lineage_blocked(root, fact.id):
                            continue
                        if fact.evidence_bound and not resolver.assess(fact.id).eligible:
                            continue
                        ranked.append((fact, cosine))
                return _dense_select(ranked, k)
            except (ImportError, RuntimeError, ValueError, OSError):
                # Optional/index failures retain the established exact fallback.
                pass

    facts = hierarchical.list_facts(root, scope=scope, as_of=as_of)  # validity, forgetting and TTL applied
    try:
        scores = fact_embeddings.scores(
            question, facts, model_path=model_path or None, model=model or None, root=root
        ) or {}
    except (ImportError, RuntimeError, ValueError, OSError):
        # A cache-only optional dense default must never suppress fact recall.
        scores = {}
    return _dense_select([(f, scores.get(f.id, 0.0)) for f in facts], k)


def _hybrid(lexical: list, dense: dict, k: int) -> list:
    """Reciprocal-rank fusion of the lexical and dense fact rankings (dense-only hits included).

    Each fact keeps its lexical score when it has one; a dense-only hit scores
    its cosine scaled into the lexical range, so the retrieval assessment never
    reads a semantic match as stronger evidence than a lexical one.
    """
    if not dense:
        return lexical
    fused: dict[str, float] = {}
    by_id: dict = {}
    for rank, (fact, score) in enumerate(item for item in lexical if item[1] > 0):
        fused[fact.id] = fused.get(fact.id, 0.0) + 1 / (RRF_K + rank + 1)
        by_id[fact.id] = (fact, score)
    for rank, (fid, (fact, cosine)) in enumerate(sorted(dense.items(), key=lambda kv: (-kv[1][1], kv[0]))):
        fused[fid] = fused.get(fid, 0.0) + 1 / (RRF_K + rank + 1)
        by_id.setdefault(fid, (fact, cosine * DENSE_ONLY_SCALE))
    order = sorted(fused, key=lambda fid: (-fused[fid], -dense.get(fid, (None, 0.0))[1], fid))[:k]
    return [by_id[fid] for fid in order]


def _function_words() -> frozenset:
    from commontrace.conversation.profile import STOPWORDS

    return frozenset(STOPWORDS)


def _facts(
    root: str, question: str, as_of: str | None, k: int, evidence_budget: int = 0, scope: str = "",
    fact_scorer: str = "overlap-v1",
) -> list[Item]:
    from commontrace import fact_index, hierarchical
    from commontrace.fact_evidence import EvidenceResolver

    ranked = hierarchical.search_facts(root, question, as_of=as_of, limit=k, scope=scope, scorer=fact_scorer)
    dense = _dense_facts(root, question, as_of, k, scope)
    ranked = _hybrid(ranked, dense, k)
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
        matched = fact_index.matched_terms(question, fact.statement, fact_scorer)
        if fact.id not in dense and matched and not set(matched) - _function_words():
            continue  # overlap on "the"/"is" alone is not relevance, and would inject unrelated facts
        provenance = {"search": {"scorer": fact_scorer, "matched_terms": matched}}
        if fact.id in dense:
            provenance["search"]["dense_cosine"] = round(dense[fact.id][1], 4)
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
    # Reviewed hazards are always delivered, ahead of and outside the budget.
    order = {i.id: n for n, i in enumerate(items)}
    chosen: list[Item] = [item for item in items if item.provenance.get("hazard")]
    used = sum(tokens(item.text) for item in chosen)
    items = [item for item in items if not item.provenance.get("hazard")]
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


def rerank_fused(question: str, fused: list[Item], reranker, *, depth: int, blend: float) -> tuple[list[Item], dict]:
    """Rerank the head of a fused ranking; see `commontrace.reranking.stage`.

    Fused scores are rank-preserving: each item takes the fused score of the
    position it now holds, so diversity and packing follow the new order and
    the scale against the untouched tail is unchanged.
    """
    from commontrace import reranking

    keys = [str(n) for n in range(len(fused))]
    order, report = reranking.stage(question, keys, {k: item.text for k, item in zip(keys, fused)}, reranker,
                                    depth=depth, blend=blend)
    values = [item.fused for item in fused]
    reordered = [fused[int(k)] for k in order]
    for item, value in zip(reordered, values):
        item.fused = value
    for row in report.get("top", []):
        row["id"] = fused[int(row["id"])].id
    return reordered, report


def weak_coverage(question: str, texts: list[str]) -> str:
    """Why `texts` cover `question` too thinly to stop at (lexical coverage), or ""."""
    from commontrace.conversation.coverage import assess

    coverage = assess(question, texts)
    if coverage.abstain:
        return "abstain: " + coverage.reason
    if coverage.confidence < LOW_CONFIDENCE:
        return f"coverage {coverage.confidence} below {LOW_CONFIDENCE}"
    if coverage.missing_subject_terms:
        return "missing subject terms: " + ", ".join(coverage.missing_subject_terms)
    return ""


def _weak_evidence(question: str, items: list[Item], assessment: RetrievalAssessment) -> str:
    """Why the packed evidence looks too thin to stop at, or "" when it does not."""
    if assessment.abstain:
        return "abstain: " + assessment.reason
    if assessment.confidence < LOW_CONFIDENCE:
        return f"confidence {assessment.confidence} below {LOW_CONFIDENCE}"
    return weak_coverage(question, [item.text for item in items])


def _fits(items: list[Item], budget: int) -> bool:
    return sum(tokens(i.text) for i in items) <= budget


def recall(root: str, question: str, *, budget: int | None = None, agent: str | None = None,
           channels: tuple[str, ...] = CHANNELS, as_of: str | None = None, weights: dict[str, float] | None = None,
           spaces: list[str] | None = None, embedder: str = "auto", per_channel: int = 12,
           evidence_budget: int = 0, scope: str = "", fact_scorer: str = "overlap-v1",
           reranker: str | None = None, rerank_depth: int | None = None, rerank_blend: float | None = None,
           adaptive_budget: bool | None = None, max_budget: int | None = None) -> Result:
    """Recall across channels; opt into bounded fact source quotes.

    Nonempty ``scope`` restricts lessons/facts to that scope or public memory.
    Graph reads are withheld until the graph supports scoped authorization.
    Conversation spaces must be explicitly selected by the authorized caller
    for scoped reads; no scoped request enumerates the root's other spaces.
    Empty scope preserves trusted-local root-wide behavior.

    ``reranker`` (a name from `providers.reranker`, or "none") reranks the top
    ``rerank_depth`` fused items before diversity and packing, blended with the
    fused order by ``rerank_blend`` (0 keeps it, 1 takes the reranker's).
    ``adaptive_budget`` sizes the budget with `conversation.search.budget_for`
    and doubles it, up to ``max_budget``, while evidence stays weak and
    unpacked candidates remain. Unset arguments fall back to `memory/budgets.json`,
    then to no reranker and a fixed budget. ``explain`` reports each decision.
    """
    from commontrace import completeness, lesson_cache, reranking

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
    settings = retrieval_settings(root, agent)
    adaptive = settings["adaptive_budget"] if adaptive_budget is None else adaptive_budget
    if not isinstance(adaptive, bool):
        raise ValueError("adaptive_budget must be true or false")
    depth = settings["depth"] if rerank_depth is None else rerank_depth
    blend = settings["blend"] if rerank_blend is None else rerank_blend
    reranking.check_options(depth, blend)
    name = settings["reranker"] if reranker is None else reranker
    ranker, unavailable = reranking.load(name)
    decision: dict | None = None
    if adaptive:
        from commontrace.conversation.search import budget_for

        ceiling = settings["max_budget"] if max_budget is None else max_budget
        if ceiling is not None and (isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 50):
            raise ValueError("max_budget must be an integer of at least 50")
        cap = max(total, min(200_000, ADAPTIVE_CAP if ceiling is None else ceiling))
        requested = total
        total, shape = budget_for(question, total, cap)
        decision = {"requested": requested, "shaped": total, "effective": total, "reason": shape, "cap": cap,
                    "grown": []}
        if total > requested:  # a wider page needs a deeper pool to fill it from
            per_channel = min(MAX_PER_CHANNEL, max(per_channel, math.ceil(per_channel * total / requested)))
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
        fused = fuse(rankings, weights)
        if ranker is not None and fused:
            with telemetry.span("recall.rerank", reranker=ranker.name):
                fused, result.explain["rerank"] = rerank_fused(question, fused, ranker, depth=depth, blend=blend)
        elif unavailable:
            result.explain["rerank"] = {"reranker": name, "items": 0, "latency_ms": 0.0, "error": unavailable}
        candidates = diversify(fused)
        from commontrace import conformal_gate, holdout_io, safety

        hazards = safety.protected(root)
        for item in candidates:
            if _memory_id(item.id) in hazards:
                item.provenance["hazard"] = True

        gate = conformal_gate.current(root)
        if gate.get("enabled"):
            # The budget is calibrated on randomized lesson injections, on the
            # relevance scale and scorer the holdout log records. Only the lessons
            # channel shares that population; other channels are not randomized
            # injections and are reported as outside the budget, never gated on
            # an incomparable fused score.
            try:
                context = holdout_io.context_label(agent or "")
            except ValueError:
                context = ""
            candidates = [item for item in candidates if item.channel != "lessons" or conformal_gate.admit(
                gate, conformal_gate.score(float(item.provenance.get("relevance", 0))), context=context,
                scorer=item.provenance.get("scorer"), hazard=bool(item.provenance.get("hazard")))]
            result.explain["harm_budget"] = {**conformal_gate.summary(gate, context),
                                             "scope": "lessons channel; other channels are not randomized injections"}
        result.items = pack(candidates, total)
        result.assessment = _assess_retrieval(question, result.items, result.errors)
        if decision is not None:
            while total < decision["cap"] and len(decision["grown"]) < MAX_GROWTH_STEPS:
                why = _weak_evidence(question, result.items, result.assessment)
                if not why:
                    break
                if _fits(candidates, total):
                    decision["stopped"] = "every candidate already fits; a larger budget adds nothing"
                    break
                grown = min(decision["cap"], total * 2)
                result.items = pack(candidates, grown)
                result.assessment = _assess_retrieval(question, result.items, result.errors)
                decision["grown"].append({"from": total, "to": grown, "why": why})
                total = grown
            decision["effective"] = result.budget = total
            result.explain["budget"] = decision
        result.explain["completeness"] = completeness.grade_question(question, result.context)
        # Weighted RRF is useful for diagnosing ranking changes, but it is not
        # a calibrated probability. Keep it explicitly separate from the
        # heuristic evidence assessment; do not manufacture a recall-hit label.
        scores = sorted((item.fused for item in result.items), reverse=True)
        top_score = scores[0] if scores else 0.0
        score_margin = scores[0] - scores[1] if len(scores) > 1 else 0.0
        handle.set(
            tokens=result.tokens,
            items=len(result.items),
            confidence=result.assessment.confidence,
            recall_top_score_raw=top_score,
            recall_score_margin_raw=score_margin,
            recall_score_scale="weighted_rrf",
            recall_assessment_confidence=result.assessment.confidence,
            recall_abstained=result.assessment.abstain,
        )
    telemetry.observe("commontrace_recall_tokens", float(result.tokens))
    return result
