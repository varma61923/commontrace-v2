"""Session summaries: the few sentences that carry a session, with their dates.
Extractive by default (no model, nothing invented); written by the configured model
when there is one and it is asked for."""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from itertools import chain

from commontrace.conversation import profile
from commontrace.conversation.store import ConversationError, Store, Turn

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
