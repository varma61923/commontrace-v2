"""Working-memory assembly: one labelled, budgeted context for the next model call.

`assemble` packs four sections, in this order and within one token budget:

1. **Core memory** -- the memory blocks in effect for this session and agent
   (`memory_blocks.resolved_blocks`: session > agent > global), capped at
   ``BLOCK_SHARE`` of the budget.
2. **Session summary** -- the session's rolling summary (`conversation.summary.rolling`),
   or its stored summary when no rolling state exists, capped at ``SUMMARY_SHARE``.
3. **Recent turns** -- the last ``recent_turns`` messages of the session, verbatim,
   capped at ``RECENT_SHARE``; the oldest are dropped first.
4. **Retrieved evidence** -- older messages from conversation recall
   (`commontrace.conversation.recall`) for the question, minus anything already in
   the recent window or repeated word for word, in whatever budget is left.

The first three sections are capped at fixed shares; whatever they leave unused
goes to the evidence section, which receives all slack. The result carries an
``explain`` dict that says what each section was allotted, what it spent, what
was dropped and what was deduplicated.

Nothing here writes: assembly is a pure read of the store (pass
``refresh_summary=True`` to fold new turns into the rolling summary first). For a
given store state, question and parameters, the output is identical on every call.
Token counts use the conversation layer's estimate (~4 characters per token).
"""
from __future__ import annotations

import dataclasses
import hashlib
import re

from commontrace import memory_blocks
from commontrace.conversation import ConversationError, Options, Store, recall
from commontrace.conversation.search import tokens
from commontrace.conversation.store import Turn

DEFAULT_BUDGET = 2000
MIN_BUDGET = 50
MAX_BUDGET = 32_000
DEFAULT_RECENT = 6
MAX_RECENT = 200
BLOCK_SHARE = 0.25
SUMMARY_SHARE = 0.15
RECENT_SHARE = 0.40
EVIDENCE_EXCERPT = 600  # longest single evidence message, in characters
SECTION_LABELS = {
    "core_blocks": "## Core memory",
    "session_summary": "## Session summary",
    "recent_turns": "## Recent turns (verbatim)",
    "evidence": "## Retrieved evidence",
}
_ELLIPSIS = " …"


def _clip(text: str, max_tokens: int) -> tuple[str, bool]:
    """Cut text to at most `max_tokens` estimated tokens; True when something was cut."""
    if tokens(text) <= max_tokens:
        return text, False
    chars = max(0, max_tokens * 4 - len(_ELLIPSIS))
    return (text[:chars].rstrip() + _ELLIPSIS) if chars else "", True


def _when(turn: Turn) -> str:
    return turn.at.strftime("%Y-%m-%d %H:%M") if turn.at else "undated"


def _turn_line(turn: Turn, *, annotated: bool) -> str:
    body = turn.annotated() if annotated else turn.text
    return f"[{_when(turn)} · {turn.session}] {turn.speaker}: {body}"


def _text_key(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.strip().lower()).encode("utf-8")).hexdigest()


def _section(name: str, lines: list[str]) -> str:
    return SECTION_LABELS[name] + "\n" + "\n".join(lines) if lines else ""


def _blocks_section(root: str, session: str, agent: str, cap: int) -> tuple[list[str], list[dict], list[str]]:
    blocks = memory_blocks.resolved_blocks(root, session=session, agent=agent)
    # Most specific first, so a tight budget keeps the session's own notes.
    rank = {scope: i for i, scope in enumerate(memory_blocks.resolution_order(session, agent))}
    blocks.sort(key=lambda b: (rank.get(b.scope, len(rank)), b.name))
    header = tokens(SECTION_LABELS["core_blocks"] + "\n")
    lines, items, dropped = [], [], []
    spent = header
    for block in blocks:
        content = block.content.strip()
        if not content:
            continue
        label = f"[{block.name}{' · ' + block.scope if block.scope else ''}] "
        room = cap - spent - tokens(label) - 1
        if room <= 0:
            dropped.append(block.name)
            continue
        body, cut = _clip(content, room)
        if not body:
            dropped.append(block.name)
            continue
        line = label + body
        lines.append(line)
        spent += tokens(line + "\n")
        items.append({"name": block.name, "scope": block.scope or "global", "revision": block.revision,
                      "truncated": cut})
    return lines, items, dropped


def _summary_text(store: Store, session: str) -> tuple[str, dict]:
    from commontrace.conversation import summary

    state = summary.rolling_state(store, session)
    if state and state.get("text"):
        return str(state["text"]), {"source": "rolling", "through_idx": state.get("through_idx"),
                                    "covers_turns": state.get("turns"), "method": state.get("method")}
    stored = store.db.execute("SELECT text, method, turns FROM summaries WHERE session=?", (session,)).fetchone()
    if stored and stored[0]:
        return str(stored[0]), {"source": "stored", "method": stored[1], "covers_turns": stored[2]}
    return "", {"source": "none"}


def assemble(root: str, space: str, session: str, question: str, *, budget: int = DEFAULT_BUDGET,
             recent_turns: int = DEFAULT_RECENT, agent: str = "", now=None,
             refresh_summary: bool = False, options: Options | None = None,
             block_namespace: str = "") -> dict:
    """The working memory for the next call in `session`, within `budget` tokens.

    ``options`` tunes the evidence recall (its ``budget`` is replaced by what is
    left); by default summaries are left out of recall (the session summary has
    its own section) and reranking is off, so assembly stays cheap and repeatable.
    ``block_namespace`` prefixes the session and agent block scopes
    (``session:<namespace>/<session>``), so tenants sharing one store never read
    each other's scoped blocks. Raises ``ConversationError`` when the space has
    no conversations.
    """
    if isinstance(budget, bool) or not isinstance(budget, int) or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise ConversationError(f"budget must be an integer from {MIN_BUDGET} to {MAX_BUDGET}")
    if isinstance(recent_turns, bool) or not isinstance(recent_turns, int) or not 0 <= recent_turns <= MAX_RECENT:
        raise ConversationError(f"recent_turns must be an integer from 0 to {MAX_RECENT}")
    if not isinstance(question, str) or not question.strip():
        raise ConversationError("question must be a non-empty string")
    explain: dict = {"budget": budget, "allocated": {}, "spent": {}, "dropped": {}, "deduplicated": {}}
    sections: list[dict] = []

    def remaining() -> int:
        return budget - sum(explain["spent"].values())

    # 1. core memory blocks
    cap = min(remaining(), int(budget * BLOCK_SHARE))
    explain["allocated"]["core_blocks"] = cap
    prefix = block_namespace + "/" if block_namespace else ""
    try:
        block_lines, block_items, dropped = _blocks_section(
            root, prefix + session if session else "", prefix + agent if agent else "", cap)
    except memory_blocks.MemoryBlockError as exc:
        raise ConversationError(str(exc)) from None
    text = _section("core_blocks", block_lines)
    explain["spent"]["core_blocks"] = tokens(text)
    explain["dropped"]["core_blocks"] = dropped
    sections.append({"name": "core_blocks", "label": SECTION_LABELS["core_blocks"], "text": text,
                     "tokens": tokens(text), "items": block_items})

    with Store(root, space, create=False, read_only=not refresh_summary) as store:
        if refresh_summary and store.db.execute("SELECT 1 FROM turns WHERE session=? LIMIT 1",
                                                (session,)).fetchone():
            from commontrace.conversation import summary

            summary.rolling(store, session)
        with store.read_snapshot():
            total = store.db.execute("SELECT COUNT(*) FROM turns WHERE session=?", (session,)).fetchone()[0]
            explain["session_turns"] = total

            # 2. session summary
            cap = min(remaining(), int(budget * SUMMARY_SHARE))
            explain["allocated"]["session_summary"] = cap
            summary_text, summary_info = _summary_text(store, session) if total else ("", {"source": "none"})
            room = cap - tokens(SECTION_LABELS["session_summary"] + "\n")
            body, cut = _clip(summary_text, room) if summary_text and room > 0 else ("", bool(summary_text))
            text = _section("session_summary", [body] if body else [])
            summary_info["truncated"] = cut
            explain["summary"] = summary_info
            explain["spent"]["session_summary"] = tokens(text)
            sections.append({"name": "session_summary", "label": SECTION_LABELS["session_summary"], "text": text,
                             "tokens": tokens(text), "items": [summary_info] if body else []})

            # 3. the last N turns, verbatim; the oldest go first when they do not fit
            cap = min(remaining(), int(budget * RECENT_SHARE))
            explain["allocated"]["recent_turns"] = cap
            recent: list[Turn] = []
            if recent_turns and total:
                rows = store.db.execute("SELECT idx FROM turns WHERE session=? ORDER BY idx DESC LIMIT ?",
                                        (session, recent_turns)).fetchall()
                if rows:
                    recent = store.session_turns(session, after_idx=rows[-1][0] - 1)
            spent = tokens(SECTION_LABELS["recent_turns"] + "\n")
            kept: list[tuple[Turn, str, bool]] = []
            for turn in reversed(recent):
                line = _turn_line(turn, annotated=False)
                need = tokens(line + "\n")
                if spent + need <= cap:
                    kept.append((turn, line, False))
                    spent += need
                elif not kept and cap - spent > 8:
                    # Never return an empty window for an overlong last message.
                    clipped, _ = _clip(line, cap - spent - 1)
                    kept.append((turn, clipped, True))
                    spent += tokens(clipped + "\n")
                else:
                    break
            kept.reverse()
            text = _section("recent_turns", [line for _t, line, _c in kept])
            explain["dropped"]["recent_turns"] = len(recent) - len(kept)
            explain["spent"]["recent_turns"] = tokens(text)
            sections.append({"name": "recent_turns", "label": SECTION_LABELS["recent_turns"], "text": text,
                             "tokens": tokens(text),
                             "items": [{"turn": t.id, "idx": t.idx, "speaker": t.speaker, "truncated": c}
                                       for t, _line, c in kept]})
            in_window = {t.id for t, _l, _c in kept}
            seen_text = {_text_key(t.text) for t, _l, _c in kept}

            # 4. retrieved evidence, in everything that is left
            cap = remaining()
            explain["allocated"]["evidence"] = cap
            header = tokens(SECTION_LABELS["evidence"] + "\n")
            lines: list[str] = []
            items: list[dict] = []
            dup_window = dup_text = dropped_evidence = 0
            recall_explain: dict = {}
            if cap - header >= MIN_BUDGET // 2 and store.turn_count():
                opts = dataclasses.replace(options or Options(summaries=False, rerank=None),
                                           budget=max(MIN_BUDGET, cap - header))
                result = recall(store, question, now=now, options=opts)
                recall_explain = {"turns": list(result.turns), "tokens": result.tokens,
                                  "window": result.window}
                found = store.turns(result.turns)
                spent = header
                for turn_id in result.turns:
                    turn = found.get(turn_id)
                    if turn is None:
                        continue
                    if turn.id in in_window:
                        dup_window += 1
                        continue
                    key = _text_key(turn.text)
                    if key in seen_text:
                        dup_text += 1
                        continue
                    line, cut = _clip(_turn_line(turn, annotated=True), EVIDENCE_EXCERPT // 4)
                    need = tokens(line + "\n")
                    if spent + need > cap:
                        dropped_evidence += 1
                        continue
                    seen_text.add(key)
                    lines.append(line)
                    spent += need
                    items.append({"turn": turn.id, "session": turn.session, "idx": turn.idx,
                                  "speaker": turn.speaker, "truncated": cut})
            text = _section("evidence", lines)
            explain["recall"] = recall_explain
            explain["deduplicated"] = {"in_recent_turns": dup_window, "repeated_text": dup_text}
            explain["dropped"]["evidence"] = dropped_evidence
            explain["spent"]["evidence"] = tokens(text)
            sections.append({"name": "evidence", "label": SECTION_LABELS["evidence"], "text": text,
                             "tokens": tokens(text), "items": items})

    context = "\n\n".join(s["text"] for s in sections if s["text"])
    explain["unused"] = max(0, budget - sum(explain["spent"].values()))
    return {"space": space, "session": session, "agent": agent, "question": question, "budget": budget,
            "tokens": tokens(context), "context": context, "sections": sections, "explain": explain}


def sessions(store: Store, *, limit: int = 200, after_seq: int | None = None) -> dict:
    """A space's sessions in order, each with its turn count, time range and summary state."""
    from commontrace.conversation import summary

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ConversationError("limit must be an integer from 1 to 1000")
    with store.read_snapshot():
        rows = store.db.execute(
            "SELECT s.id, s.started_at, s.seq, COUNT(t.id) AS turns, MIN(t.at) AS first_at, MAX(t.at) AS last_at, "
            "MAX(t.idx) AS last_idx FROM sessions s LEFT JOIN turns t ON t.session = s.id "
            "WHERE (? IS NULL OR s.seq > ?) GROUP BY s.id ORDER BY s.seq LIMIT ?",
            (after_seq, after_seq, limit + 1)).fetchall()
        current = store.summaries()
        out = []
        for row in rows[:limit]:
            state = summary.rolling_state(store, row["id"])
            stored = current.get(row["id"])
            out.append({"id": row["id"], "seq": row["seq"], "started_at": row["started_at"],
                        "turns": row["turns"], "first_at": row["first_at"], "last_at": row["last_at"],
                        "summary": {"current": stored is not None,
                                    "method": stored["method"] if stored else None,
                                    "rolling_through_idx": state.get("through_idx") if state else None,
                                    "pending_turns": (row["turns"] - int(state.get("turns", 0))) if state
                                    else row["turns"]}})
        total = store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return {"space": store.space, "sessions": out, "total": total,
            "next_after": out[-1]["seq"] if len(rows) > limit and out else None}
