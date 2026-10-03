"""Conversation memory: sessions and turns in one SQLite file per space, each turn's
relative dates grounded as it is written, searchable with FTS5 BM25."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from commontrace import memory_guard, paths
from commontrace.conversation import profile, timeparse

SPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_RE = re.compile(r"^[^\x00-\x1f]{1,200}$")
MAX_TURN_CHARS = 32_000
UNIT_CHARS = 700
SCHEMA_VERSION = 1


class ConversationError(ValueError):
    """A request the conversation store refuses."""


def conversations_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "conversations")


def db_path(root: str, space: str) -> str:
    if not SPACE_RE.match(space or "") or space.startswith("."):
        raise ConversationError(f"space must match {SPACE_RE.pattern}, got {space!r}")
    return os.path.join(conversations_dir(root), f"{space}.db")


def spaces(root: str) -> list[str]:
    try:
        names = os.listdir(conversations_dir(root))
    except OSError:
        return []
    return sorted(n[:-3] for n in names if n.endswith(".db") and SPACE_RE.match(n[:-3]))


def _fts5_available() -> bool:
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    return True


FTS5 = _fts5_available()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, started_at TEXT, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, idx INTEGER NOT NULL,
    at TEXT, speaker TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
    dates TEXT NOT NULL DEFAULT '[]', lo TEXT, hi TEXT, ref TEXT, key TEXT NOT NULL UNIQUE,
    UNIQUE (session, idx));
CREATE INDEX IF NOT EXISTS turns_at ON turns (at);
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    part INTEGER NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS units_turn ON units (turn);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL, at TEXT);
CREATE INDEX IF NOT EXISTS facts_kind ON facts (kind, subject);
"""


@dataclass(frozen=True)
class Turn:
    id: int
    session: str
    idx: int
    at: dt.datetime | None
    speaker: str
    role: str
    text: str
    dates: tuple[timeparse.Grounding, ...]
    ref: str | None = None

    def annotated(self) -> str:
        return timeparse.annotate(self.text, list(self.dates))


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.isoformat(timespec="minutes") if moment else None


def _moment(value) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo is None else \
            value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    moment = timeparse.parse_moment(str(value))
    if moment is None:
        raise ConversationError(f"unrecognised date {value!r}")
    return moment


def split_units(text: str, limit: int = UNIT_CHARS) -> list[str]:
    """Retrieval units: the turn itself, or sentence-aligned windows of a long one."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+|\n{2,}|\n(?=[-*\d])", text)
    units, current = [], ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                units.append(current)
                current = ""
            units.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            units.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        units.append(current)
    return units


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _fts_query(text: str) -> str:
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if t not in profile.STOPWORDS]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


class Store:
    """One space's conversations. Thread-safe; one writer at a time per file."""

    def __init__(self, root: str, space: str, *, create: bool = True):
        self.root, self.space = root, space
        self.path = db_path(root, space)
        if not create and not os.path.isfile(self.path):
            raise ConversationError(f"no conversations stored for space {space!r}")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=NORMAL")
        with self.db:
            self.db.executescript(_SCHEMA)
            if FTS5:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING "
                                "fts5(body, tokenize='porter unicode61')")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        self._turn_cache: dict[int, Turn] = {}

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- writing -----------------------------------------------------------------

    def add(self, session: str, messages: Iterable[Mapping], *, session_at=None,
            user_speakers: Iterable[str] = ()) -> dict:
        """Append messages to a session. Re-adding a message already stored is a no-op."""
        if not SESSION_RE.match(session or ""):
            raise ConversationError("session id must be 1-200 printable characters")
        started = _moment(session_at)
        users = {s.lower() for s in user_speakers}
        added = skipped = redacted = 0
        with self._lock, self.db:
            row = self.db.execute("SELECT started_at FROM sessions WHERE id=?", (session,)).fetchone()
            if row is None:
                seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM sessions").fetchone()[0]
                self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (session, _iso(started), seq))
            elif started and not row["started_at"]:
                self.db.execute("UPDATE sessions SET started_at=? WHERE id=?", (_iso(started), session))
            elif not started and row["started_at"]:
                started = dt.datetime.fromisoformat(row["started_at"])
            idx = self.db.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM turns WHERE session=?",
                                  (session,)).fetchone()[0]
            for message in messages:
                text = str(message.get("text", message.get("content", "")) or "").strip()
                if not text:
                    skipped += 1
                    continue
                if len(text) > MAX_TURN_CHARS:
                    raise ConversationError(f"a message is {len(text)} characters; the limit is {MAX_TURN_CHARS}")
                text, found = memory_guard.redact_secrets(text)
                redacted += len(found)
                role = str(message.get("role") or "").strip().lower()
                speaker = str(message.get("speaker") or message.get("name") or role or "user").strip()[:120]
                if not role:
                    role = "user" if not users or speaker.lower() in users else "other"
                at = _moment(message.get("at") or message.get("timestamp")) or started
                ref = message.get("id")
                ref = str(ref)[:200] if ref not in (None, "") else None
                key = _hash("\x1f".join([session, "ref", ref]) if ref else
                            "\x1f".join([session, speaker, _iso(at) or "", text]))
                if self.db.execute("SELECT 1 FROM turns WHERE key=?", (key,)).fetchone():
                    skipped += 1
                    continue
                groundings = timeparse.ground(text, at)
                dates = json.dumps([[g.start, g.end, g.label, g.lo.isoformat(), g.hi.isoformat()]
                                    for g in groundings])
                lo = min((g.lo for g in groundings), default=None)
                hi = max((g.hi for g in groundings), default=None)
                cur = self.db.execute(
                    "INSERT INTO turns (session, idx, at, speaker, role, text, dates, lo, hi, ref, key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session, idx, _iso(at), speaker, role, text, dates,
                     lo.isoformat() if lo else None, hi.isoformat() if hi else None, ref, key))
                turn_id = cur.lastrowid
                annotated = timeparse.annotate(text, groundings)
                for part, unit in enumerate(split_units(annotated)):
                    body = f"{speaker}: {unit}"
                    uid = self.db.execute("INSERT INTO units (turn, part, body, hash) VALUES (?, ?, ?, ?)",
                                          (turn_id, part, body, _hash(body))).lastrowid
                    if FTS5:
                        self.db.execute("INSERT INTO units_fts (rowid, body) VALUES (?, ?)", (uid, body))
                if role == "user":
                    for fact in profile.extract(text):
                        self.db.execute("INSERT INTO facts (turn, kind, subject, statement, at) VALUES (?, ?, ?, ?, ?)",
                                        (turn_id, fact.kind, fact.subject, fact.statement, _iso(at)))
                idx += 1
                added += 1
        return {"space": self.space, "session": session, "added": added, "skipped": skipped,
                "secrets_redacted": redacted}

    def delete_session(self, session: str) -> int:
        with self._lock, self.db:
            ids = [r[0] for r in self.db.execute(
                "SELECT u.id FROM units u JOIN turns t ON t.id = u.turn WHERE t.session=?", (session,))]
            if FTS5:
                self.db.executemany("DELETE FROM units_fts WHERE rowid=?", [(i,) for i in ids])
            n = self.db.execute("DELETE FROM turns WHERE session=?", (session,)).rowcount
            self.db.execute("DELETE FROM sessions WHERE id=?", (session,))
        self._turn_cache.clear()
        return n

    # --- reading -----------------------------------------------------------------

    def stats(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {"space": self.space, "sessions": one("SELECT COUNT(*) FROM sessions"),
                "turns": one("SELECT COUNT(*) FROM turns"), "units": one("SELECT COUNT(*) FROM units"),
                "facts": one("SELECT COUNT(*) FROM facts"),
                "first": one("SELECT MIN(at) FROM turns"), "last": one("SELECT MAX(at) FROM turns")}

    def sessions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT s.id, s.started_at, COUNT(t.id) AS turns FROM sessions s "
            "LEFT JOIN turns t ON t.session = s.id GROUP BY s.id ORDER BY s.seq")]

    def latest_moment(self) -> dt.datetime | None:
        value = self.db.execute("SELECT MAX(at) FROM turns").fetchone()[0]
        return dt.datetime.fromisoformat(value) if value else None

    def _row_turn(self, r) -> Turn:
        dates = tuple(timeparse.Grounding(s, e, "", lab, dt.date.fromisoformat(lo), dt.date.fromisoformat(hi))
                      for s, e, lab, lo, hi in json.loads(r["dates"]))
        return Turn(r["id"], r["session"], r["idx"], dt.datetime.fromisoformat(r["at"]) if r["at"] else None,
                    r["speaker"], r["role"], r["text"], dates, r["ref"])

    def turns(self, ids: Iterable[int]) -> dict[int, Turn]:
        ids = list(dict.fromkeys(ids))
        missing = [i for i in ids if i not in self._turn_cache]
        if missing:
            for r in self.db.execute("SELECT * FROM turns WHERE id IN (SELECT value FROM json_each(?))",
                                     (json.dumps(missing),)):
                self._turn_cache[r["id"]] = self._row_turn(r)
        return {i: self._turn_cache[i] for i in ids if i in self._turn_cache}

    def neighbours(self, turn: Turn, before: int, after: int) -> list[int]:
        return [r[0] for r in self.db.execute(
            "SELECT id FROM turns WHERE session=? AND idx BETWEEN ? AND ? AND idx != ? ORDER BY idx",
            (turn.session, turn.idx - before, turn.idx + after, turn.idx))]

    def units(self) -> list[tuple[int, int, str, str]]:
        """Every retrieval unit: (unit id, turn id, body, content hash)."""
        return [tuple(r) for r in self.db.execute("SELECT id, turn, body, hash FROM units ORDER BY id")]

    def unit_turns(self, unit_ids: Iterable[int]) -> dict[int, int]:
        ids = list(unit_ids)
        if not ids:
            return {}
        return dict(self.db.execute("SELECT id, turn FROM units WHERE id IN (SELECT value FROM json_each(?))",
                                    (json.dumps(ids),)).fetchall())

    def lexical(self, query: str, limit: int) -> list[tuple[int, float]]:
        """(unit id, score) by BM25, best first."""
        match = _fts_query(query)
        if not match:
            return []
        if FTS5:
            return [(r[0], -r[1]) for r in self.db.execute(
                "SELECT rowid, bm25(units_fts) FROM units_fts WHERE units_fts MATCH ? "
                "ORDER BY bm25(units_fts) LIMIT ?", (match, limit))]
        return _python_bm25(self.units(), query, limit)

    def in_window(self, lo: dt.date, hi: dt.date) -> set[int]:
        """Turns said within [lo, hi], or whose grounded dates overlap it."""
        lo_s, hi_s = lo.isoformat(), (hi + dt.timedelta(days=1)).isoformat()
        return {r[0] for r in self.db.execute(
            "SELECT id FROM turns WHERE (at >= ? AND at < ?) OR (lo IS NOT NULL AND lo < ? AND hi >= ?)",
            (lo_s, hi_s, hi_s, lo.isoformat()))}

    def facts(self, kinds: Iterable[str] = ()) -> list[dict]:
        kinds = list(kinds)
        return [dict(r) for r in self.db.execute(
            "SELECT f.*, t.session FROM facts f JOIN turns t ON t.id = f.turn "
            "WHERE ? = '[]' OR f.kind IN (SELECT value FROM json_each(?)) ORDER BY f.at, f.id",
            (json.dumps(kinds), json.dumps(kinds)))]


def _python_bm25(units, query: str, limit: int) -> list[tuple[int, float]]:
    import math

    from commontrace._stem import stem

    def terms(text):
        return [stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in profile.STOPWORDS]

    q = set(terms(query))
    docs = [(uid, terms(body)) for uid, _turn, body, _h in units]
    if not docs or not q:
        return []
    avg = sum(len(d) for _u, d in docs) / len(docs)
    df = {t: sum(1 for _u, d in docs if t in d) for t in q}
    scored = []
    for uid, d in docs:
        score = 0.0
        for t in q:
            tf = d.count(t)
            if tf:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))
        if score > 0:
            scored.append((uid, score))
    return sorted(scored, key=lambda x: -x[1])[:limit]
