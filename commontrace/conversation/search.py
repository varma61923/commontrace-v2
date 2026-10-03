"""Recall: the turns that answer a question, assembled into a dated, token-budgeted
context. Lexical and semantic arms are fused per sub-query and combined by their
best rank (a turn that is first for one facet keeps that), a question's time
window lifts the turns said or set in it, and the page is filled best-first with
each hit's neighbouring turns, then shown in the order things were said."""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field

from commontrace import injection_guard
from commontrace.conversation import profile, timeparse
from commontrace.conversation.store import Store, Turn

DEFAULT_BUDGET = 1500
RRF_K = 60
POOL = 200


@dataclass
class Options:
    budget: int = DEFAULT_BUDGET
    pool: int = POOL
    neighbours_before: int = 2
    neighbours_after: int = 2
    neighbour_hits: int | None = None
    window_boost: float = 1.0
    entity_boost: float = 0.1
    lexical_weight: float = 0.5
    rerank: str | None = "auto"
    rerank_depth: int = 50
    profile_facts: int = 4
    embedder: str | None = "auto"
    summaries: bool = True
    sessions: tuple[str, ...] = ()
    speakers: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None


@dataclass
class Recall:
    question: str
    context: str
    tokens: int
    turns: list[int]
    ranked: list[int]
    window: tuple[str, str, str] | None
    explain: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"question": self.question, "context": self.context, "tokens": self.tokens,
                "turns": self.turns, "window": self.window, "explain": self.explain}


def tokens(text: str) -> int:
    """The context's size in tokens, estimated the way most tokenizers land: ~4 chars each."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


_ADVICE = re.compile(r"\b(?:recommend|suggest|suggestions?|ideas?|tips?|advice|should I|what should|"
                     r"any (?:good|other)|help me (?:choose|pick|find|plan))\b", re.I)


def subqueries(question: str) -> list[str]:
    """The question, plus each clause of a compound one."""
    out = [question]
    parts = re.split(r"\s*(?:;|,\s*and\b|\band then\b|\balso\b)\s*", question)
    if len(parts) > 1:
        out += [p for p in parts if len(re.findall(r"[A-Za-z]{3,}", p)) >= 2]
    return list(dict.fromkeys(out))


def _rrf(rankings: list[tuple[list[int], float]]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking, weight in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + weight / (RRF_K + rank + 1)
    return scores


def _embedder(store: Store, choice: str | None):
    from commontrace.conversation import embed

    tag = embed.configured() if choice == "auto" else choice
    if not tag:
        return None
    if not embed.available():
        return None
    return embed.Embedder(store.root, tag)


def _header(session: str, at: dt.datetime | None) -> str:
    if at is None:
        return f"[{session}]"
    when = f"{timeparse.WEEKDAYS[at.weekday()].capitalize()} {timeparse.label(at.date())}"
    if at.hour or at.minute:
        when += at.strftime(", %H:%M")
    return f"[{session} · {when}]"


def _summary_line(text: str) -> str:
    return f"(session summary: {text})"


def _line(turn: Turn) -> str:
    return f"{turn.speaker}: {turn.annotated()}"


def recall(store: Store, question: str, *, now=None, options: Options | None = None,
           extra_queries: list[str] = ()) -> Recall:
    """`extra_queries` are searched beside the question, each keeping its own best
    ranks (a follow-up search that finds a missing fact first is not diluted)."""
    from commontrace import telemetry

    with telemetry.span("conversation.recall", space=store.space, queries=1 + len(extra_queries)) as handle:
        result = _recall(store, question, now=now, options=options, extra_queries=extra_queries)
        handle.set(tokens=result.tokens, turns=len(result.turns))
        return result


def _recall(store: Store, question: str, *, now=None, options: Options | None = None,
            extra_queries: list[str] = ()) -> Recall:
    opts = options or Options()
    question = (question or "").strip()
    if not question:
        return Recall(question, "", 0, [], [], None)
    moment = timeparse.parse_moment(now) if isinstance(now, str) else now
    moment = moment or store.latest_moment()
    window = timeparse.question_window(question, moment)
    embedder = _embedder(store, opts.embedder)
    qvec_cache: dict[str, object] = {}
    allowed = store.allowed(sessions=opts.sessions, speakers=opts.speakers, since=opts.since,
                            until=opts.until, now=moment)
    pool = opts.pool if allowed is None else opts.pool * 10

    def arm_rankings(query: str) -> list[tuple[list[int], float]]:
        lexical = [u for u, _s in store.lexical(query, pool)]
        if embedder is None:
            return [(lexical, 1.0)]
        from commontrace.conversation import embed

        if query not in qvec_cache:
            qvec_cache[query] = embedder.encode([query], query=True)[0]
        dense = [u for u, _s in embed.search(store, embedder, qvec_cache[query], pool)]
        return [(dense, 1.0), (lexical, opts.lexical_weight)]

    def turn_scores(queries: list[str]) -> dict[int, float]:
        by_turn: dict[int, float] = {}
        for query in queries:
            ranked_turns = []
            for units, weight in arm_rankings(query):
                unit_turn = store.unit_turns(units)
                ranked_turns.append((list(dict.fromkeys(
                    unit_turn[u] for u in units if u in unit_turn and (allowed is None or unit_turn[u] in allowed))),
                    weight))
            for turn, score in _rrf(ranked_turns).items():
                by_turn[turn] = max(by_turn.get(turn, 0.0), score)
        return by_turn

    queries = list(dict.fromkeys(subqueries(question) + [q.strip() for q in extra_queries if q and q.strip()]))
    scores = turn_scores(queries)
    explain: dict = {"subqueries": queries}
    if allowed is not None:
        explain["filtered_to"] = len(allowed)
    if opts.entity_boost and scores:
        named = profile.entities(question)
        if named:
            top = max(scores.values())
            boosted = 0
            for name, turns in store.entity_turns(named).items():
                weight = 1.0 / (1.0 + 0.001 * (len(turns) - 1) ** 2) if turns else 0.0
                for turn in turns:
                    if allowed is None or turn in allowed:
                        scores[turn] = scores.get(turn, 0.0) + opts.entity_boost * top * weight
                        boosted += 1
            explain["entities"] = sorted(named)
    if window is not None:
        inside = store.in_window(window[0], window[1])
        inside = inside if allowed is None else inside & allowed
        explain["window_turns"] = len(inside)
        top = max(scores.values(), default=0.0)
        for turn in inside:
            if turn in scores:
                scores[turn] += opts.window_boost * top * 0.5
    ranked = sorted(scores, key=lambda t: (-scores[t], t))
    rerank = opts.rerank
    if rerank == "auto":
        rerank = "cross-encoder" if embedder is not None else None
    if rerank and ranked:
        ranked = _rerank(store, question, ranked, rerank, opts.rerank_depth)
        explain["rerank"] = rerank
    withheld: list[int] = []
    context, used, n_tokens = assemble(store, question, ranked, opts, withheld, allowed)
    if withheld:
        explain["withheld"] = withheld
    return Recall(question, context, n_tokens, used, ranked,
                  (window[0].isoformat(), window[1].isoformat(), window[2]) if window else None, explain)


def _rerank(store: Store, question: str, ranked: list[int], mode: str, depth: int) -> list[int]:
    from commontrace import rerank_arm

    if rerank_arm.ready(mode):
        return ranked
    head = ranked[:depth]
    turns = store.turns(head)
    text_of = {str(t): f"{turns[t].speaker}: {turns[t].annotated()}" for t in head if t in turns}
    page, _ = rerank_arm.rerank(question, list(text_of), text_of, len(text_of), mode=mode)
    order = [int(s) for s, _x in page]
    return order + [t for t in ranked if t not in set(order)]


def _profile_lines(store: Store, question: str, limit: int) -> list[str]:
    if limit <= 0:
        return []
    facts = store.facts()
    if not facts:
        return []
    asked = set(profile.subject_of(question, limit=20).split())
    advice = bool(_ADVICE.search(question))
    scored = []
    for f in facts:
        overlap = len(asked & set(profile.subject_of(f["statement"], limit=30).split()))
        if overlap or (advice and f["kind"] in ("preference", "dislike", "favorite", "identity")):
            scored.append((overlap + (0.5 if f["kind"] in ("preference", "dislike") else 0), f))
    scored.sort(key=lambda x: x[1]["at"] or "", reverse=True)
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _score, f in scored:
        if f["statement"] in seen:
            continue
        seen.add(f["statement"])
        day = f"({f['at'][:10]}) " if f["at"] else ""
        out.append(f"- {day}{f['statement']}")
        if len(out) >= limit:
            break
    return out


def _flagged(turn: Turn) -> bool:
    return bool(injection_guard.injection_labels({"text": turn.text}))


def assemble(store: Store, question: str, ranked: list[int], opts: Options,
             withheld: list[int] | None = None, allowed: set[int] | None = None) -> tuple[str, list[int], int]:
    """Fill the budget best-first, each hit with its neighbours, then render by time.
    A turn the injection screen flags is never shown; its id goes to `withheld`."""
    withheld = [] if withheld is None else withheld
    budget = max(0, opts.budget)
    profile_lines = _profile_lines(store, question, opts.profile_facts)
    profile_block = ("[What the user has said about themselves]\n" + "\n".join(profile_lines) + "\n\n") \
        if profile_lines else ""
    spent = tokens(profile_block)
    if spent > budget // 3:
        profile_block, spent = "", 0
    chosen: dict[int, Turn] = {}
    sessions_seen: set[str] = set()
    summaries = store.summaries() if opts.summaries else {}

    def header_cost(session: str, at) -> int:
        cost = tokens(_header(session, at)) + 1
        if session in summaries:
            cost += tokens(_summary_line(summaries[session]["text"])) + 1
        return cost

    with_context = opts.neighbour_hits if opts.neighbour_hits is not None else max(5, budget // 400)
    hits = 0
    for start in range(0, len(ranked), 50):
        batch = ranked[start:start + 50]
        turns = store.turns(batch)
        full = False
        for tid in batch:
            turn = turns.get(tid)
            if turn is None or tid in chosen:
                continue
            hits += 1
            group = [tid] + [n for n in (store.neighbours(turn, opts.neighbours_before, opts.neighbours_after)
                                         if hits <= with_context else []) if allowed is None or n in allowed]
            group_turns = store.turns(group)
            for g in list(group):
                if g in group_turns and _flagged(group_turns[g]):
                    group.remove(g)
                    if g not in withheld:
                        withheld.append(g)
            if tid not in group:
                continue
            cost = sum(tokens(_line(group_turns[g])) + 1 for g in group if g not in chosen and g in group_turns)
            if turn.session not in sessions_seen:
                cost += header_cost(turn.session, turn.at)
            if spent + cost > budget:
                solo = tokens(_line(turn)) + 1 + (0 if turn.session in sessions_seen else
                                                  header_cost(turn.session, turn.at))
                if spent + solo > budget:
                    full = spent >= budget * 0.97
                    continue
                group, cost = [tid], solo
            for g in group:
                if g in group_turns:
                    chosen[g] = group_turns[g]
            sessions_seen.add(turn.session)
            spent += cost
        if full:
            break
    by_session: dict[str, list[Turn]] = {}
    for turn in sorted(chosen.values(), key=lambda t: (t.at or dt.datetime.min, t.session, t.idx)):
        by_session.setdefault(turn.session, []).append(turn)
    blocks = []
    for session, turns in sorted(by_session.items(), key=lambda kv: (kv[1][0].at or dt.datetime.min, kv[0])):
        lines = [_header(session, turns[0].at)]
        if session in summaries:
            lines.append(_summary_line(summaries[session]["text"]))
        previous = None
        for turn in sorted(turns, key=lambda t: t.idx):
            if previous is not None and turn.idx > previous + 1:
                lines.append("…")
            lines.append(_line(turn))
            previous = turn.idx
        blocks.append("\n".join(lines))
    context = profile_block + "\n\n".join(blocks)
    return context, list(chosen), tokens(context)
