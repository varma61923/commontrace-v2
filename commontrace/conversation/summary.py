"""Session summaries: the few sentences that carry a session, with their dates.
Extractive by default (no model, nothing invented); written by the configured model
when there is one and it is asked for. `rolling` keeps a growing session's summary
current from only its new turns, with a hash-chain integrity check."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from itertools import chain

from commontrace.conversation import profile
from commontrace.conversation.store import ConversationError, Store, Turn, write_txn

MAX_CHARS = 480
SENTENCES = 3
MAX_TRANSCRIPT_CHARS = 24_000
MODEL_PAGE = 16

_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in profile.STOPWORDS and len(w) > 2]


def extractive(turns: list[Turn], sentences: int = SENTENCES, limit: int = MAX_CHARS) -> str:
    """The session's most central sentences, in the order they were said."""
    candidates = []
    for turn in turns:
        for sentence in _SPLIT.split(turn.annotated()):
            words = _words(sentence)
            if len(words) >= 3 and not sentence.rstrip().endswith("?"):
                candidates.append((turn, sentence.strip(), words))
    if not candidates:
        return ""
    freq = Counter(w for _t, _s, words in candidates for w in set(words))
    scored = []
    for i, (turn, sentence, words) in enumerate(candidates):
        weight = sum(math.log(1 + freq[w]) for w in set(words)) / math.sqrt(len(set(words)))
        weight *= 1.3 if turn.dates else 1.0
        scored.append((weight, i))
    keep = sorted(i for _w, i in sorted(scored, reverse=True)[:sentences])
    out = []
    for i in keep:
        turn, sentence, _words_ = candidates[i]
        line = f"{turn.speaker}: {sentence}"
        if sum(len(x) + 1 for x in out) + len(line) > limit:
            break
        out.append(line)
    return " ".join(out)


PROMPT = """Summarise this conversation session in at most three sentences for someone who
will later answer questions about it. Keep names, decisions, plans, preferences and
facts; write every date as an absolute date (dates in [brackets] are already resolved).
Do not add anything the conversation does not say.

Session of {when}:
{transcript}

Summary:"""


def written(turns: Iterable[Turn], complete=None) -> str:
    from commontrace import llm

    complete = complete or llm.complete
    turns = iter(turns)
    first = next(turns, None)
    when = first.at.date().isoformat() if first and first.at else "unknown date"
    # Preserve the exact prompt prefix without materializing the rest of a long
    # session or annotating messages that cannot enter the model context.
    fragments, remaining = [], MAX_TRANSCRIPT_CHARS
    for turn in chain((first,), turns) if first is not None else ():
        line = ("\n" if fragments else "") + f"{turn.speaker}: {turn.annotated()}"
        fragments.append(line[:remaining])
        remaining -= len(fragments[-1])
        if not remaining:
            break
    transcript = "".join(fragments)
    text, _usage = complete(PROMPT.format(when=when, transcript=transcript))
    return " ".join(text.split())[:MAX_CHARS * 2]


def _model_turns(store: Store, session: str):
    """Read only the ordered source pages the bounded model transcript needs."""
    after = -1
    while True:
        page = store.session_turns(session, after_idx=after, limit=MODEL_PAGE)
        if not page:
            return
        yield from page
        after = page[-1].idx


def summarize(store: Store, sessions: list[str] | None = None, *, method: str = "extractive",
              force: bool = False, complete=None) -> dict:
    """Write a summary for each session that has none (or has grown since its last one)."""
    if method not in ("extractive", "model"):
        raise ConversationError("method must be extractive or model")
    existing = store.summaries()
    targets = sessions or [s["id"] for s in store.sessions()]
    written_n, skipped = 0, 0
    for session in targets:
        known = existing.get(session)
        # summaries() already verifies source turn counts using the session
        # index. A valid cached summary needs no raw message hydration.
        if known and not force:
            skipped += 1
            continue
        with store.read_snapshot():
            revision = store.unit_stamp()
            if not store.db.execute("SELECT 1 FROM turns WHERE session=? LIMIT 1", (session,)).fetchone():
                raise ConversationError(f"no session {session!r} in space {store.space!r}")
            text = written(_model_turns(store, session), complete) if method == "model" else \
                extractive(store.session_turns(session))
        if text:
            store.set_summary(session, text, method, expected_revision=revision)
            written_n += 1
    return {"space": store.space, "summarized": written_n, "unchanged": skipped, "method": method}


# --- rolling (incremental) session summaries -------------------------------------
#
# A growing session is not re-summarised from scratch on every turn. The rolling
# state lives in the space's ``meta`` table under ``rolling-summary:<session>``:
#
#   through_idx / through_turn  the high-water mark: the last turn folded in
#   turns                       how many turns the summary covers
#   chain                       sha256 hash chain over every covered turn's
#                               ``evidence_hash`` (content, time, speaker, id)
#   freq, pool, order           the extractive path's bounded working set
#
# An update first proves the covered prefix is unchanged (the chain is recomputed
# from stored rows: hashing only, no summarisation); an edit, purge or re-import
# of an earlier turn breaks the chain and the summary is rebuilt from scratch.
# Otherwise only turns after the high-water mark are read and folded in.
#
# The extractive path is deterministic and dependency-free. Folding everything in
# at once reproduces ``extractive()`` exactly; folding in increments keeps the
# ROLLING_POOL most central sentences seen so far, so a sentence that fell out of
# the pool cannot return -- the price of bounded work per update. The model path
# hands the previous summary and only the new turns to the configured model.

ROLLING_KEY = "rolling-summary:"
ROLLING_VERSION = 1
ROLLING_POOL = 12
ROLLING_VOCAB = 4096
ROLLING_PAGE = 256

ROLLING_PROMPT = """Update the running summary of a conversation session with the new messages
below. Return at most three sentences for someone who will later answer questions
about the session. Keep names, decisions, plans, preferences and facts from both the
running summary and the new messages; when a new message changes an earlier fact,
keep the new one. Write every date as an absolute date (dates in [brackets] are
already resolved). Do not add anything the conversation does not say.

Running summary:
{summary}

New messages:
{transcript}

Updated summary:"""


def _chain(previous: str, turn: Turn) -> str:
    return hashlib.sha256((previous + "\x1f" + turn.evidence_hash()).encode("utf-8")).hexdigest()


def rolling_state(store: Store, session: str) -> dict | None:
    """The stored rolling state for a session, or None when there is none (or it is unreadable)."""
    raw = store.get_meta(ROLLING_KEY + session)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    return state if isinstance(state, dict) and state.get("version") == ROLLING_VERSION else None


def _pages(store: Store, session: str, *, after_idx: int = -1, through_idx: int | None = None):
    after = after_idx
    while True:
        page = store.session_turns(session, after_idx=after, through_idx=through_idx, limit=ROLLING_PAGE)
        if not page:
            return
        yield from page
        after = page[-1].idx


def _prefix_intact(store: Store, session: str, state: dict, verify: str) -> bool:
    """Whether the turns the summary covers are exactly the turns stored now."""
    through = int(state.get("through_idx", -1))
    if through < 0:
        return int(state.get("turns", 0)) == 0
    count = store.db.execute("SELECT COUNT(*) FROM turns WHERE session=? AND idx<=?",
                             (session, through)).fetchone()[0]
    if count != int(state.get("turns", -1)):
        return False
    if verify == "tail":
        last = store.session_turns(session, after_idx=through - 1, through_idx=through, limit=1)
        return bool(last) and last[0].evidence_hash() == state.get("tail")
    chain_hash = ""
    for turn in _pages(store, session, through_idx=through):
        chain_hash = _chain(chain_hash, turn)
    return chain_hash == state.get("chain")


def _candidates(turn: Turn) -> list[tuple[int, str, list[str]]]:
    out = []
    for pos, sentence in enumerate(_SPLIT.split(turn.annotated())):
        words = _words(sentence)
        if len(words) >= 3 and not sentence.rstrip().endswith("?"):
            out.append((pos, sentence.strip(), sorted(set(words))))
    return out


def _weight(entry: dict, freq: dict) -> float:
    words = entry["words"]
    weight = sum(math.log(1 + freq.get(w, 0)) for w in words) / math.sqrt(len(words))
    return weight * (1.3 if entry["dated"] else 1.0)


def _fold_extractive(state: dict, turns: Iterable[Turn], sentences: int, limit: int) -> str:
    freq: dict[str, int] = state.setdefault("freq", {})
    pool: list[dict] = state.setdefault("pool", [])
    order = int(state.get("order", 0))
    for turn in turns:
        for pos, sentence, words in _candidates(turn):
            for w in words:
                freq[w] = freq.get(w, 0) + 1
            pool.append({"idx": turn.idx, "pos": pos, "order": order, "speaker": turn.speaker,
                         "sentence": sentence, "words": words, "dated": bool(turn.dates)})
            order += 1
    state["order"] = order
    if len(freq) > ROLLING_VOCAB:
        state["freq"] = freq = dict(sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:ROLLING_VOCAB])
    # The same ranking as extractive(): weight first, the later sentence wins a tie.
    ranked = sorted(pool, key=lambda e: (-_weight(e, freq), -e["order"]))
    state["pool"] = ranked[:ROLLING_POOL]
    out: list[str] = []
    for entry in sorted(ranked[:sentences], key=lambda e: e["order"]):
        line = f"{entry['speaker']}: {entry['sentence']}"
        if sum(len(x) + 1 for x in out) + len(line) > limit:
            break
        out.append(line)
    return " ".join(out)


def _transcript(turns: Iterable[Turn]) -> str:
    fragments, remaining = [], MAX_TRANSCRIPT_CHARS
    for turn in turns:
        line = ("\n" if fragments else "") + f"{turn.speaker}: {turn.annotated()}"
        fragments.append(line[:remaining])
        remaining -= len(fragments[-1])
        if not remaining:
            break
    return "".join(fragments)


def rolling(store: Store, session: str, *, method: str = "extractive", complete=None, force: bool = False,
            verify: str = "full", sentences: int = SENTENCES, limit: int = MAX_CHARS) -> dict:
    """Bring one session's rolling summary up to date, reading only turns it has not seen.

    ``mode`` in the result is ``unchanged`` (nothing new), ``incremental`` (only
    new turns were summarised) or ``full`` (first run, forced, method changed, or
    the covered turns changed -- ``reason`` says which). ``verify="tail"`` checks
    only the covered count and the last covered turn instead of the whole chain.
    The text is also stored as the session's summary (method ``rolling-<method>``),
    so recall shows it under the session header.
    """
    if method not in ("extractive", "model"):
        raise ConversationError("method must be extractive or model")
    if verify not in ("full", "tail"):
        raise ConversationError("verify must be full or tail")
    for _attempt in range(3):
        try:
            return _rolling_once(store, session, method, complete, force, verify, sentences, limit)
        except ConversationError as exc:
            if "changed during" not in str(exc):
                raise
    raise ConversationError("session kept changing during summarization; retry")


def _rolling_once(store: Store, session: str, method: str, complete, force: bool, verify: str,
                  sentences: int, limit: int) -> dict:
    with store.read_snapshot():
        revision = store.unit_stamp()
        total = store.db.execute("SELECT COUNT(*) FROM turns WHERE session=?", (session,)).fetchone()[0]
        if not total:
            raise ConversationError(f"no session {session!r} in space {store.space!r}")
        state = rolling_state(store, session)
        reason = ""
        if force:
            reason = "forced"
        elif state is None:
            reason = "first"
        elif state.get("method") != method:
            reason = "method-changed"
        elif not _prefix_intact(store, session, state, verify):
            reason = "history-changed"
        if reason or state is None:
            previous_text = ""
            state = {"version": ROLLING_VERSION, "method": method, "through_idx": -1, "through_turn": None,
                     "turns": 0, "chain": "", "tail": ""}
        else:
            previous_text = str(state.get("text") or "")
        new = list(_pages(store, session, after_idx=int(state["through_idx"])))
        if not new and not reason:
            return {"space": store.space, "session": session, "method": method, "mode": "unchanged",
                    "reason": "", "new_turns": 0, "turns": int(state["turns"]),
                    "through_idx": int(state["through_idx"]), "text": previous_text}
        chain_hash = str(state.get("chain") or "")
        for turn in new:
            chain_hash = _chain(chain_hash, turn)
        if method == "model":
            from commontrace import llm

            complete_fn = complete or llm.complete
            if previous_text:
                text, _usage = complete_fn(ROLLING_PROMPT.format(summary=previous_text,
                                                                 transcript=_transcript(new)))
                text = " ".join(text.split())[:MAX_CHARS * 2]
            else:
                text = written(new, complete_fn)
        else:
            text = _fold_extractive(state, new, sentences, limit)
    if new:
        state.update(through_idx=new[-1].idx, through_turn=new[-1].id, tail=new[-1].evidence_hash())
    state.update(turns=int(state["turns"]) + len(new), chain=chain_hash, text=text, method=method,
                 updated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    with store._lock, write_txn(store.db):
        if store.unit_stamp() != revision:
            raise ConversationError("session evidence changed during summarization; retry")
        store.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                         (ROLLING_KEY + session, json.dumps(state, ensure_ascii=False, sort_keys=True)))
        if text:
            store.set_summary(session, text, "rolling-" + method, expected_revision=revision)
    return {"space": store.space, "session": session, "method": method,
            "mode": "full" if reason else "incremental", "reason": reason, "new_turns": len(new),
            "turns": state["turns"], "through_idx": state["through_idx"], "text": text}


def rolling_all(store: Store, sessions: list[str] | None = None, *, method: str = "extractive",
                complete=None, force: bool = False, verify: str = "full") -> dict:
    """``rolling`` for each session (all by default), with counts by mode."""
    targets = sessions or [s["id"] for s in store.sessions()]
    counts = {"unchanged": 0, "incremental": 0, "full": 0}
    results = []
    for session in targets:
        out = rolling(store, session, method=method, complete=complete, force=force, verify=verify)
        counts[out["mode"]] += 1
        results.append({k: out[k] for k in ("session", "mode", "reason", "new_turns", "turns")})
    return {"space": store.space, "method": method, **counts, "sessions": results}
