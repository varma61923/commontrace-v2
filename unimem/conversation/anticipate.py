"""Anticipatory indexing: precompute the questions each memory will be asked.

People rarely ask in the words they spoke. "What might John's degree be in?" shares
no word with "I've been loving my policy classes". Spending model time while the
store is idle (sleep-time compute) closes that gap: for each turn, a configured LLM
writes the short questions the turn answers, with names resolved ("I" becomes the
speaker), and the keywords a searcher would use. They are indexed as extra search
units of that same turn.

Generated text is a retrieval key, never evidence: units at or above
``EXPANSION_PART`` are not shown in any context (excerpts and context lines are
always the turn's own text), and they are deleted with their turn, so forgetting,
expiry and session deletion remove them too. Output that trips the injection screen
is discarded. Without a configured LLM nothing runs.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import closing
from typing import Any

from unimem import injection_guard
from unimem.conversation.store import FTS5, Store, _hash, spaces, write_txn
from unimem.parallel import bounded_imap

EXPANSION_PART = 1_000_000  # generated units of a turn use parts from here up
BATCH = 12
MAX_TURN_CHARS = 700
MAX_EXPANSION_CHARS = 600

PROMPT = """You are indexing a conversation memory so that later questions can find it.
For each numbered turn below, write:
- "questions": 3 short questions that this turn answers or helps answer, written the way
  someone would later ask about it. Use the speakers' names instead of I/you/we, and
  absolute dates where the turn gives them.
- "keywords": up to 6 words or short phrases a searcher might use that are NOT already
  in the turn (synonyms, categories, implied topics).
If a turn carries no information (greetings, filler), return empty lists for it.
Do not invent facts that the turn does not state or clearly imply.

{turns}

Return JSON only: a list of objects {{"id": <turn number>, "questions": [...], "keywords": [...]}}."""


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS anticipated (turn INTEGER PRIMARY KEY "
    "REFERENCES turns(id) ON DELETE CASCADE) WITHOUT ROWID"
)


def _ensure(store: Store) -> None:
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='anticipated'").fetchone() is None:
        with write_txn(store.db):
            store.db.execute(_SCHEMA)


def pending(store: Store, *, limit: int | None = None) -> list[int]:
    """Turns not yet processed, oldest first."""
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be a nonnegative integer or None")
    sql = "SELECT t.id FROM turns t WHERE NOT EXISTS (SELECT 1 FROM anticipated a WHERE a.turn = t.id) ORDER BY t.id"
    params: list[int] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with store._lock:
        _ensure(store)
        return [row[0] for row in store.db.execute(sql, params)]


def _render(store: Store, batch: list[int]) -> tuple[str, dict[int, int], dict[int, str]]:
    turns = store.turns(batch)
    sessions = {
        row[0]: row[1]
        for row in store.db.execute(
            "SELECT id, started_at FROM sessions WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted({t.session for t in turns.values()})),),
        )
    }
    lines, numbering, fingerprints = [], {}, {}
    for n, turn_id in enumerate(batch, 1):
        turn = turns.get(turn_id)
        if turn is None:
            continue
        numbering[n] = turn_id
        fingerprints[turn_id] = turn.evidence_hash()
        when = turn.at.date().isoformat() if turn.at else (sessions.get(turn.session) or "undated")[:10]
        text = " ".join(turn.text.split())[:MAX_TURN_CHARS]
        lines.append(f"[{n}] ({when}) {turn.speaker}: {text}")
    return "\n".join(lines), numbering, fingerprints


def _parse(text: str) -> list[dict[str, Any]]:
    lo, hi = text.find("["), text.rfind("]")
    if lo < 0 or hi <= lo:
        return []
    try:
        items = json.loads(text[lo : hi + 1])
    except (ValueError, RecursionError):
        return []
    return [item for item in items if isinstance(item, dict)]


def _expansion(item: dict[str, Any]) -> str:
    parts = []
    for key in ("questions", "keywords"):
        values = item.get(key)
        if isinstance(values, list):
            parts += [" ".join(str(v).split()) for v in values if isinstance(v, (str, int, float)) and str(v).strip()]
    body = " ".join(parts)[:MAX_EXPANSION_CHARS]
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", body).strip()


def expand(
    store: Store,
    complete: Callable[[str], tuple[str, dict[str, Any]]],
    *,
    limit: int | None = None,
    batch: int = BATCH,
    workers: int = 1,
) -> dict[str, int]:
    """Generate and index anticipated questions for up to `limit` pending turns.

    `complete(prompt) -> (text, usage)` is the configured LLM; `workers` model calls
    run concurrently while writes stay serial. Each turn is marked done even when the
    model gives it nothing, so a pass never repeats work; a batch the model fails on
    is left pending for the next pass."""
    report = {"turns": 0, "indexed": 0, "empty": 0, "rejected": 0, "failed_batches": 0}
    todo = pending(store, limit=limit)

    def requests() -> Iterator[tuple[str, dict[int, int], dict[int, str]]]:
        for i in range(0, len(todo), max(1, batch)):
            with store._lock:
                text, numbering, fingerprints = _render(store, todo[i : i + max(1, batch)])
            if numbering:
                yield PROMPT.format(turns=text), numbering, fingerprints

    def ask(request: tuple[str, dict[int, int], dict[int, str]]) -> tuple[dict[int, int], dict[int, str], str | None]:
        prompt, numbering, fingerprints = request
        try:
            answer = complete(prompt)[0]
        except Exception:  # noqa: BLE001 - provider errors leave the batch pending
            answer = None
        return numbering, fingerprints, answer

    with closing(bounded_imap(ask, requests(), max_workers=max(1, workers))) as answers:
        for numbering, fingerprints, answer in answers:
            produced = {}
            for item in _parse(answer or ""):
                identity = item.get("id")
                if isinstance(identity, bool) or not isinstance(identity, (int, str)):
                    continue
                try:
                    n = int(identity)
                except ValueError:
                    continue
                if n in numbering:
                    produced[numbering[n]] = _expansion(item)
            if not produced:
                report["failed_batches"] += 1
                continue
            _write(store, numbering, produced, report, fingerprints)
    return report


def _write(
    store: Store,
    numbering: dict[int, int],
    produced: dict[int, str],
    report: dict[str, int],
    fingerprints: dict[int, str],
) -> None:
    with store._lock, write_txn(store.db):
        current = store.turns(numbering.values())
        for turn_id in numbering.values():
            turn = current.get(turn_id)
            if turn is None or turn.evidence_hash() != fingerprints[turn_id]:
                continue
            # Claim only a live, still-pending turn under the write lock. A
            # competing pass or deletion can win while the model is running.
            claimed = store.db.execute(
                "INSERT OR IGNORE INTO anticipated (turn) SELECT id FROM turns WHERE id=?", (turn_id,)
            ).rowcount
            if not claimed:
                continue
            body = produced.get(turn_id, "")
            if body and injection_guard.injection_labels({"text": body}):
                report["rejected"] += 1
                body = ""
            if body:
                uid = store.db.execute(
                    "INSERT INTO units (turn, part, body, hash) VALUES (?, ?, ?, ?)",
                    (turn_id, EXPANSION_PART, body, _hash(body)),
                ).lastrowid
                if FTS5:
                    store.db.execute("INSERT INTO units_fts (rowid, body) VALUES (?, ?)", (uid, body))
            report["turns"] += 1
            report["indexed" if body else "empty"] += 1


PASS_LIMIT = 240  # turns per space per idle pass


def enabled() -> bool:
    value = os.environ.get("UNIMEM_ANTICIPATE", "off").strip().lower()
    if value not in {"on", "off"}:
        raise ValueError("UNIMEM_ANTICIPATE must be on or off")
    return value == "on"


def run_if_enabled(
    root: str, *, complete: Callable[[str], tuple[str, dict[str, Any]]] | None = None, limit: int = PASS_LIMIT
) -> dict[str, dict[str, int]] | None:
    """The idle-pass hook: index up to `limit` pending turns per conversation space with
    the configured LLM. Opt-in (UNIMEM_ANTICIPATE=on) because it spends model calls."""
    if not enabled():
        return None
    if complete is None:
        from unimem import llm

        llm.load_config()
        complete = llm.complete
    out = {}
    for space in spaces(root):
        with Store(root, space) as store:
            if pending(store, limit=1):
                out[space] = expand(store, complete, limit=limit)
    return out
