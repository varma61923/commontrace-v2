"""Conversation memory: sessions and turns in one SQLite file per space, each turn's
relative dates grounded as it is written, searchable with FTS5 BM25."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from commontrace import memory_guard, paths
from commontrace._stem import stem
from commontrace.conversation import profile, timeparse

SPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_RE = re.compile(r"^[^\x00-\x1f]{1,200}$")
MAX_TURN_CHARS = 1_000_000  # long pastes are split into retrieval units, not refused
UNIT_CHARS = 700
SCHEMA_VERSION = 2
FACT_KINDS = ("instruction", "preference", "dislike", "favorite", "identity", "habit", "plan", "possession",
              "event", "fact", "relationship")


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
    expires TEXT, UNIQUE (session, idx));
CREATE INDEX IF NOT EXISTS turns_at ON turns (at);
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    part INTEGER NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS units_turn ON units (turn);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL, at TEXT,
    slot TEXT, source TEXT NOT NULL DEFAULT 'rule', superseded_by INTEGER);
CREATE INDEX IF NOT EXISTS facts_kind ON facts (kind, subject);
CREATE TABLE IF NOT EXISTS entities (
    name TEXT NOT NULL, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    PRIMARY KEY (name, turn)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS entities_turn ON entities (turn);
CREATE TABLE IF NOT EXISTS summaries (
    session TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    text TEXT NOT NULL, method TEXT NOT NULL, turns INTEGER NOT NULL, at TEXT NOT NULL);
"""


BUSY_SECONDS = 30.0


def _retry_locked(fn, deadline: float):
    """Run fn, retrying while SQLite reports the file locked (journal-mode changes and
    schema setup on a brand-new file do not always wait on the busy timeout)."""
    delay = 0.01
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def connect(path: str, setup: str = "", *, read_only: bool = False) -> sqlite3.Connection:
    """A WAL connection in autocommit mode; write through `write_txn`. A read-only
    connection opens the file with mode=ro and query_only, so no statement can write."""
    if read_only:
        if not os.path.isfile(path):
            raise ConversationError(f"no such store file: {path}")
        uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=BUSY_SECONDS, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA query_only=ON")
        return db
    db = sqlite3.connect(path, timeout=BUSY_SECONDS, check_same_thread=False, isolation_level=None)
    deadline = time.monotonic() + BUSY_SECONDS
    _retry_locked(lambda: db.execute("PRAGMA journal_mode=WAL"), deadline)
    db.execute("PRAGMA synchronous=NORMAL")
    # EverOS engine pattern: scratch tables in memory, an 8MB page cache bound
    # so one pathological session cannot balloon process RSS.
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-8192")
    if setup:
        def _setup():
            with write_txn(db):
                for statement in filter(str.strip, setup.split(";")):
                    db.execute(statement)
        _retry_locked(_setup, deadline)
    return db


@contextlib.contextmanager
def write_txn(db: sqlite3.Connection):
    """One write transaction holding the write lock from its first statement, so a
    read-then-write inside it cannot race another writer."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


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
        return value.replace(tzinfo=None)  # the speaker's wall clock, as for parsed text
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


def fact_hash(statement: str) -> str:
    """MD5 of the normalized fact statement for cheap cross-turn dedup.

    Normalization (adapted from Mem0's additive-extraction dedup): lowercase,
    strip surrounding whitespace/punctuation, collapse internal whitespace.
    Two turns stating the same fact with different casing/spacing share a hash.
    """
    norm = re.sub(r"\s+", " ", str(statement or "").strip().lower().strip(" .,;:!?\"'"))
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


def sigmoid_bm25(raw: float, n_query_terms: int) -> float:
    """Normalise a raw BM25 score to [0, 1] with a query-length adaptive sigmoid.

    Adapts Mem0's ``normalize_bm25`` formula (mem0/utils/scoring.py) which maps
    unbounded BM25 scores (typically 0–20+) to the same scale as cosine
    similarity (0–1) so they can be combined additively without rank-only loss.

    Lives here (not in ``search``) so the store can normalize scores without a
    circular import: ``search`` imports this module.
    """
    if n_query_terms <= 3:
        midpoint, steepness = 5.0, 0.7
    elif n_query_terms <= 6:
        midpoint, steepness = 7.0, 0.6
    elif n_query_terms <= 9:
        midpoint, steepness = 9.0, 0.5
    elif n_query_terms <= 15:
        midpoint, steepness = 10.0, 0.5
    else:
        midpoint, steepness = 12.0, 0.5
    return 1.0 / (1.0 + math.exp(-steepness * (raw - midpoint)))


def _fts_query(text: str) -> str:
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if t not in profile.STOPWORDS]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


class Store:
    """One space's conversations. Thread-safe; one writer at a time per file."""

    def __init__(self, root: str, space: str, *, create: bool = True, read_only: bool = False):
        self.root, self.space, self.read_only = root, space, read_only
        self.path = db_path(root, space)
        if (not create or read_only) and not os.path.isfile(self.path):
            raise ConversationError(f"no conversations stored for space {space!r}")
        self._lock = threading.RLock()
        self._turn_cache: dict[int, Turn] = {}
        if read_only:
            # frozen memory: recall works, every write raises sqlite3.OperationalError
            self.db = connect(self.path, read_only=True)
            self.db.row_factory = sqlite3.Row
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fts = ("CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(body, tokenize='porter unicode61');"
               if FTS5 else "")
        self.db = connect(self.path, _SCHEMA + fts +
                          f"INSERT OR IGNORE INTO meta VALUES ('schema', '{SCHEMA_VERSION}')")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        """Bring a file written by an older version up to this schema, in place."""
        wanted = {"turns": [("expires", "TEXT")],
                  "facts": [("slot", "TEXT"), ("source", "TEXT NOT NULL DEFAULT 'rule'"),
                            ("superseded_by", "INTEGER")]}
        missing = [(table, col, decl) for table, cols in wanted.items() for col, decl in cols
                   if col not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}]
        if self.get_meta("entities") != "1":
            with self._lock, write_txn(self.db):
                if self.get_meta("entities") != "1":
                    for tid, text in self.db.execute("SELECT id, text FROM turns").fetchall():
                        self.db.executemany("INSERT OR IGNORE INTO entities VALUES (?, ?)",
                                            [(e, tid) for e in profile.entities(text)])
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES ('entities', '1')")
        if not missing:
            return
        with self._lock, write_txn(self.db):
            for table, col, decl in missing:
                if col not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.db.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))

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
        with self._lock, write_txn(self.db):
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
                pii_text, pii_found = memory_guard.redact_pii(text)
                if pii_found:
                    text = pii_text
                    redacted += len(pii_found)
                role = str(message.get("role") or "").strip().lower()
                speaker = str(message.get("speaker") or message.get("name") or role or "user").strip()[:120]
                if not role:
                    role = "user" if not users or speaker.lower() in users else "other"
                at = _moment(message.get("at") or message.get("timestamp")) or started
                expires = _moment(message.get("expires"))
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
                    "INSERT INTO turns (session, idx, at, speaker, role, text, dates, lo, hi, ref, key, expires) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session, idx, _iso(at), speaker, role, text, dates,
                     lo.isoformat() if lo else None, hi.isoformat() if hi else None, ref, key, _iso(expires)))
                turn_id = cur.lastrowid
                self.db.executemany("INSERT OR IGNORE INTO entities VALUES (?, ?)",
                                    [(e, turn_id) for e in profile.entities(text)])
                annotated = timeparse.annotate(text, groundings)
                for part, unit in enumerate(split_units(annotated)):
                    body = f"{speaker}: {unit}"
                    uid = self.db.execute("INSERT INTO units (turn, part, body, hash) VALUES (?, ?, ?, ?)",
                                          (turn_id, part, body, _hash(body))).lastrowid
                    if FTS5:
                        self.db.execute("INSERT INTO units_fts (rowid, body) VALUES (?, ?)", (uid, body))
                if role == "user":
                    for fact in profile.extract(text):
                        self._insert_fact(turn_id, fact.kind, fact.subject, fact.statement, _iso(at),
                                          fact.slot, "rule")
                idx += 1
                added += 1
        return {"space": self.space, "session": session, "added": added, "skipped": skipped,
                "secrets_redacted": redacted}

    def delete_session(self, session: str) -> int:
        with self._lock, write_txn(self.db):
            ids = [r[0] for r in self.db.execute(
                "SELECT u.id FROM units u JOIN turns t ON t.id = u.turn WHERE t.session=?", (session,))]
            if FTS5:
                self.db.executemany("DELETE FROM units_fts WHERE rowid=?", [(i,) for i in ids])
            n = self.db.execute("DELETE FROM turns WHERE session=?", (session,)).rowcount
            self.db.execute("DELETE FROM sessions WHERE id=?", (session,))
            self._reinstate()
        self._turn_cache.clear()
        return n

    def _reinstate(self) -> None:
        """A statement whose replacement was deleted is current again."""
        self.db.execute("UPDATE facts SET superseded_by=NULL WHERE superseded_by IS NOT NULL "
                        "AND superseded_by NOT IN (SELECT id FROM facts)")

    def _insert_fact(self, turn: int, kind: str, subject: str, statement: str, at: str | None,
                     slot: str | None, source: str) -> int:
        fid = self.db.execute(
            "INSERT INTO facts (turn, kind, subject, statement, at, slot, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn, kind, subject, statement, at, slot, source)).lastrowid
        if slot:
            self.db.execute("UPDATE facts SET superseded_by=? WHERE slot=? AND id != ? AND superseded_by IS NULL "
                            "AND COALESCE(at, '') <= COALESCE(?, '')", (fid, slot, fid, at))
        return fid

    def add_memories(self, session: str, memories: Iterable[Mapping], *, source: str) -> int:
        """Store memories distilled from a session (by a model or by hand), each tied to
        the session's latest turn so deleting the session deletes them."""
        added = 0
        with self._lock, write_txn(self.db):
            row = self.db.execute("SELECT id, at FROM turns WHERE session=? ORDER BY idx DESC LIMIT 1",
                                  (session,)).fetchone()
            if row is None:
                raise ConversationError(f"no session {session!r} in space {self.space!r}")
            # One snapshot up front: exact statements + MD5 hashes of everything
            # stored, so per-memory dedup is O(1) instead of a full-table scan
            # per memory (cognee ingest_data / mem0 existing_hashes pattern).
            seen_statements = {r[0] for r in self.db.execute("SELECT statement FROM facts")}
            seen_hashes = {fact_hash(s) for s in seen_statements}
            for m in memories:
                raw = str(m.get("text") or "").strip()[:profile.MAX_STATEMENT]
                text, _found = memory_guard.redact_secrets(raw)
                pii_text, _pii = memory_guard.redact_pii(text)
                text = pii_text
                kind = str(m.get("kind") or "fact").strip().lower()
                if not text or kind not in FACT_KINDS:
                    continue
                if text in seen_statements or fact_hash(text) in seen_hashes:
                    continue
                seen_statements.add(text)
                seen_hashes.add(fact_hash(text))
                slot = str(m["slot"]).strip().lower()[:80] if m.get("slot") else None
                self._insert_fact(row["id"], kind, profile.subject_of(text), text,
                                  str(m.get("at") or row["at"] or "") or None, slot, source)
                added += 1
        return added

    def set_summary(self, session: str, text: str, method: str) -> None:
        with self._lock, write_txn(self.db):
            n = self.db.execute("SELECT COUNT(*) FROM turns WHERE session=?", (session,)).fetchone()[0]
            if not n:
                raise ConversationError(f"no session {session!r} in space {self.space!r}")
            self.db.execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?, ?)",
                            (session, text, method, n, _iso(dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))))

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, write_txn(self.db):
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def purge(self, *, before=None, expired_at=None) -> int:
        """Delete turns said before `before`, or whose expiry has passed at `expired_at`."""
        clauses, args = [], []
        if before is not None:
            clauses.append("at < ?")
            args.append(_iso(_moment(before)))
        if expired_at is not None:
            clauses.append("(expires IS NOT NULL AND expires <= ?)")
            args.append(_iso(_moment(expired_at)))
        if not clauses:
            return 0
        where = " OR ".join(clauses)
        with self._lock, write_txn(self.db):
            ids = [r[0] for r in self.db.execute(
                f"SELECT u.id FROM units u JOIN turns t ON t.id = u.turn WHERE {where}", args)]  # nosec B608
            if FTS5:
                self.db.executemany("DELETE FROM units_fts WHERE rowid=?", [(i,) for i in ids])
            n = self.db.execute(f"DELETE FROM turns WHERE {where}", args).rowcount  # nosec B608
            self.db.execute("DELETE FROM sessions WHERE id NOT IN (SELECT DISTINCT session FROM turns)")
            self._reinstate()
        self._turn_cache.clear()
        return n

    # --- reading -----------------------------------------------------------------

    def stats(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {"space": self.space, "sessions": one("SELECT COUNT(*) FROM sessions"),
                "turns": one("SELECT COUNT(*) FROM turns"), "units": one("SELECT COUNT(*) FROM units"),
                "facts": one("SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL"),
                "summaries": one("SELECT COUNT(*) FROM summaries"),
                "entities": one("SELECT COUNT(DISTINCT name) FROM entities"),
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
        """(unit id, score) by BM25, best first.

        Scores are sigmoid-normalized to [0, 1] (see :func:`sigmoid_bm25`,
        adapted from Mem0's ``normalize_bm25``) so they share a scale with
        cosine similarity instead of living on an unbounded 0–20+ range.
        """
        match = _fts_query(query)
        if not match:
            return []
        n_terms = len(match.split(" OR "))
        if FTS5:
            rows = self.db.execute(
                "SELECT rowid, bm25(units_fts) FROM units_fts WHERE units_fts MATCH ? "
                "ORDER BY bm25(units_fts) LIMIT ?", (match, limit))
            return [(r[0], sigmoid_bm25(-r[1], n_terms)) for r in rows]
        return _python_bm25(self.units(), query, limit)

    def in_window(self, lo: dt.date, hi: dt.date) -> set[int]:
        """Turns said within [lo, hi], or whose grounded dates overlap it."""
        lo_s, hi_s = lo.isoformat(), (hi + dt.timedelta(days=1)).isoformat()
        return {r[0] for r in self.db.execute(
            "SELECT id FROM turns WHERE (at >= ? AND at < ?) OR (lo IS NOT NULL AND lo < ? AND hi >= ?)",
            (lo_s, hi_s, hi_s, lo.isoformat()))}

    def facts(self, kinds: Iterable[str] = (), *, history: bool = False) -> list[dict]:
        """What the user has said about themselves (and what a model distilled), oldest
        first; a statement a newer one replaced is left out unless `history`."""
        kinds = list(kinds)
        return [dict(r) for r in self.db.execute(
            "SELECT f.*, t.session FROM facts f JOIN turns t ON t.id = f.turn "
            "WHERE (? = '[]' OR f.kind IN (SELECT value FROM json_each(?))) AND (? OR f.superseded_by IS NULL) "
            "ORDER BY f.at, f.id", (json.dumps(kinds), json.dumps(kinds), int(history)))]

    def entity_turns(self, names: Iterable[str]) -> dict[str, list[int]]:
        """The turns mentioning each name or spoken by them.

        One batched query for all names (cognee single-WHERE-IN pattern),
        not one UNION per name.
        """
        names = list(dict.fromkeys(n.lower() for n in names))
        if not names:
            return {}
        out: dict[str, list[int]] = {name: [] for name in names}
        placeholders = ",".join("?" for _ in names)
        for found, turn in self.db.execute(
                f"SELECT name, turn FROM entities WHERE name IN ({placeholders})",  # nosec B608 - placeholders only
                names):
            out[found].append(turn)
        for speaker, turn in self.db.execute(
                f"SELECT LOWER(speaker), id FROM turns WHERE LOWER(speaker) IN ({placeholders})",  # nosec B608
                names):
            if turn not in out[speaker]:
                out[speaker].append(turn)
        return out

    def summaries(self) -> dict[str, dict]:
        return {r["session"]: dict(r) for r in self.db.execute("SELECT * FROM summaries")}

    def timeline(self, limit: int = 500) -> list[dict]:
        """Chronological episodic chain of sessions with their summaries, dates, and turn bounds."""
        query = """
        SELECT s.id as session, s.started_at, s.seq, sm.text as summary, COUNT(t.id) as turns,
               MIN(t.id) as first_turn, MAX(t.id) as last_turn
        FROM sessions s
        LEFT JOIN summaries sm ON sm.session = s.id
        LEFT JOIN turns t ON t.session = s.id
        GROUP BY s.id
        ORDER BY s.seq ASC
        LIMIT ?
        """
        rows = self.db.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def session_turns(self, session: str) -> list[Turn]:
        rows = self.db.execute("SELECT * FROM turns WHERE session=? ORDER BY idx", (session,)).fetchall()
        return [self._row_turn(r) for r in rows]

    def allowed(self, *, sessions: Iterable[str] = (), speakers: Iterable[str] = (), since=None, until=None,
                now: dt.datetime | None = None) -> set[int] | None:
        """The turns a filtered recall may use, or None when nothing is filtered out."""
        sessions, speakers = list(sessions), [s.lower() for s in speakers]
        clauses, args = [], []
        if sessions:
            clauses.append("session IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(sessions))
        if speakers:
            clauses.append("lower(speaker) IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(speakers))
        if since is not None:
            clauses.append("at >= ?")
            args.append(_iso(_moment(since)))
        if until is not None:
            clauses.append("at <= ?")
            args.append(_iso(_moment(until)))
        moment = _iso(now) if now else _iso(dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
        expired = self.db.execute("SELECT 1 FROM turns WHERE expires IS NOT NULL AND expires <= ? LIMIT 1",
                                  (moment,)).fetchone()
        if expired:
            clauses.append("(expires IS NULL OR expires > ?)")
            args.append(moment)
        if not clauses:
            return None
        return {r[0] for r in self.db.execute(
            "SELECT id FROM turns WHERE " + " AND ".join(clauses), args)}  # nosec B608 - fixed clauses

    def promote(self, *, scope: str = "") -> dict:
        """Copy the current profile into the store's atomic facts, so every surface that
        reads facts (MCP query_facts, the agent loop) knows it. Restating is a no-op."""
        from commontrace import hierarchical

        category = {"preference": "preference", "dislike": "preference", "favorite": "preference"}
        items = [{"statement": f["statement"], "category": category.get(f["kind"], "general"),
                  "scopes": [scope] if scope else [f"conversation:{self.space}"],
                  "valid_from": f["at"][:10] if f["at"] else None}
                 for f in self.facts()]
        results = hierarchical.add_facts(self.root, items) if items else []
        return {"space": self.space, "promoted": sum(1 for _f, action in results if action == "ADD"),
                "reinforced": sum(1 for _f, action in results if action != "ADD")}

    def export(self):
        """Every session as {session, started_at, summary, messages}, in order."""
        summaries = self.summaries()
        for row in self.sessions():
            yield {"session": row["id"], "started_at": row["started_at"],
                   "summary": (summaries.get(row["id"]) or {}).get("text"),
                   "messages": [{"id": r["ref"], "speaker": r["speaker"], "role": r["role"], "text": r["text"],
                                 "at": r["at"], "expires": r["expires"]}
                                for r in self.db.execute("SELECT * FROM turns WHERE session=? ORDER BY idx",
                                                         (row["id"],))]}


def _python_bm25(units, query: str, limit: int) -> list[tuple[int, float]]:
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
            scored.append((uid, sigmoid_bm25(score, len(q))))
    return sorted(scored, key=lambda x: -x[1])[:limit]
