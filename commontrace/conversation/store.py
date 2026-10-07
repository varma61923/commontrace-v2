"""Conversation memory: sessions and turns in one SQLite file per space, each turn's
relative dates grounded as it is written, searchable with FTS5 BM25."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from commontrace import memory_guard, paths
from commontrace._stem import stem
from commontrace.conversation import profile, timeparse

SPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_RE = re.compile(r"^[^\x00-\x1f]{1,200}$")
_WHITESPACE_RE = re.compile(r"\s+")
_BM25_WORD_RE = re.compile(r"[a-z0-9]+")
MAX_TURN_CHARS = 1_000_000  # long pastes are split into retrieval units, not refused
UNIT_CHARS = 700
SCHEMA_VERSION = 3
TURN_CACHE_SIZE = 2048
TURN_CACHE_BYTES = 16 * 1024 * 1024
FACT_KINDS = ("instruction", "preference", "dislike", "favorite", "identity", "habit", "plan", "possession",
              "event", "fact", "relationship")


class ConversationError(ValueError):
    """A request the conversation store refuses."""


def _safe_label(value: str, *, limit: int = 120) -> tuple[str, int]:
    """Scrub external labels without collapsing distinct private identities.

    Stable names are retained. Sensitive names receive a deterministic opaque
    suffix so replay and owner attribution remain stable after redaction. Role
    forgery is refused before any label can enter retrieval or profile units.
    """
    if memory_guard.scan_injection(value):
        raise ConversationError("conversation metadata contains unsafe role or instruction delimiters")
    clean, secrets = memory_guard.redact_secrets(value)
    clean, pii = memory_guard.redact_pii(clean)
    count = len(secrets) + len(pii)
    if count:
        suffix = "#" + hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:16]
        return clean[:max(0, limit - len(suffix))] + suffix, count
    return clean[:limit], 0


def _validate_session_metadata(session: str) -> None:
    # Routing IDs are never rewritten: changing one would redirect operations
    # and break callers' idempotency. Reject sensitive/forged IDs instead.
    if (memory_guard.scan_injection(session) or memory_guard.redact_secrets(session)[1]
            or memory_guard.redact_pii(session)[1]):
        raise ConversationError("session id must not contain credentials, PII, or instruction delimiters")


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
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
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
CREATE INDEX IF NOT EXISTS turns_speaker ON turns (LOWER(speaker));
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    part INTEGER NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS units_turn ON units (turn);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL, at TEXT,
    slot TEXT, source TEXT NOT NULL DEFAULT 'rule', superseded_by INTEGER,
    owner TEXT NOT NULL DEFAULT '', statement_hash TEXT);
CREATE INDEX IF NOT EXISTS facts_kind ON facts (kind, subject);
CREATE TABLE IF NOT EXISTS entities (
    name TEXT NOT NULL, turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    PRIMARY KEY (name, turn)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS entities_turn ON entities (turn);
CREATE TABLE IF NOT EXISTS summaries (
    session TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    text TEXT NOT NULL, method TEXT NOT NULL, turns INTEGER NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fact_sources (
    fact INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    turn INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    PRIMARY KEY (fact, turn)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS fact_sources_turn ON fact_sources (turn);
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
    nested = db.in_transaction
    db.execute("SAVEPOINT commontrace_write" if nested else "BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        if nested:
            db.execute("ROLLBACK TO commontrace_write")
            db.execute("RELEASE commontrace_write")
        else:
            db.execute("ROLLBACK")
        raise
    db.execute("RELEASE commontrace_write" if nested else "COMMIT")


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

    def evidence_hash(self) -> str:
        """Stable source identity, including content when SQLite recycles a turn id."""
        evidence = [self.id, self.session, self.idx, _iso(self.at), self.speaker,
                    self.role, self.text, self.ref,
                    [[g.start, g.end, g.label, g.lo.isoformat(), g.hi.isoformat()] for g in self.dates]]
        return hashlib.sha256(json.dumps(evidence, ensure_ascii=False).encode("utf-8")).hexdigest()


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
    if limit <= 0:
        raise ValueError("unit size must be positive")
    text = text.strip()
    if len(text) <= limit:
        return [text]
    # A cheap forward-character search avoids the lookbehind/alternation scan
    # for flat paragraphs and machine-generated pastes with no separator.
    sentences = re.split(r"(?<=[.!?])\s+|\n{2,}|\n(?=[-*\d])", text) \
        if any(marker in text for marker in (".", "!", "?", "\n")) else [text]
    units, current = [], ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        start, end = 0, len(sentence)
        # Keep offsets into the original sentence. Copying and stripping the
        # entire remaining suffix for every unit is quadratic on long pastes.
        while end - start > limit:
            cut = sentence.rfind(" ", start, start + limit)
            cut = cut if cut > start + limit // 2 else start + limit
            if current:
                units.append(current)
                current = ""
            units.append(sentence[start:cut].strip())
            start = cut
            while start < end and sentence[start].isspace():
                start += 1
        sentence = sentence[start:]
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
    norm = _WHITESPACE_RE.sub(" ", str(statement or "").strip().lower().strip(" .,;:!?\"'"))
    return hashlib.md5(norm.encode("utf-8"), usedforsecurity=False).hexdigest()


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
        self._turn_cache: OrderedDict[int, Turn] = OrderedDict()
        self._turn_cache_bytes = 0
        self.cache_identity = object()
        self._read_stamp = None
        self._embedders: dict = {}
        if read_only:
            # frozen memory: recall works, every write raises sqlite3.OperationalError
            self.db = connect(self.path, read_only=True)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self._inspect_schema()
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fts = ("CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(body, tokenize='porter unicode61');"
               if FTS5 else "")
        self.db = connect(self.path, _SCHEMA + fts +
                          f"INSERT OR IGNORE INTO meta VALUES ('schema', '{SCHEMA_VERSION}')")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._inspect_schema()

    def _inspect_schema(self) -> None:
        self._fact_columns = {r[1] for r in self.db.execute("PRAGMA table_info(facts)")}
        self._has_sources = bool(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='fact_sources'").fetchone())
        self._has_expiry = "expires" in {r[1] for r in self.db.execute("PRAGMA table_info(turns)")}
        self._units_identity = self.get_meta("units_identity")

    def _migrate(self) -> None:
        """Bring a file written by an older version up to this schema, in place."""
        wanted = {"turns": [("expires", "TEXT")],
                  "facts": [("slot", "TEXT"), ("source", "TEXT NOT NULL DEFAULT 'rule'"),
                            ("superseded_by", "INTEGER"), ("owner", "TEXT NOT NULL DEFAULT ''"),
                            ("statement_hash", "TEXT")]}
        missing = [(table, col, decl) for table, cols in wanted.items() for col, decl in cols
                   if col not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}]
        if self.get_meta("entities") != "1":
            with self._lock, write_txn(self.db):
                if self.get_meta("entities") != "1":
                    for tid, text in self.db.execute("SELECT id, text FROM turns"):
                        self.db.executemany("INSERT OR IGNORE INTO entities VALUES (?, ?)",
                                            [(e, tid) for e in profile.entities(text)])
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES ('entities', '1')")
        if not missing and self.get_meta("belief_chains") == str(SCHEMA_VERSION) \
                and self.get_meta("units_identity") is not None:
            return
        with self._lock, write_txn(self.db):
            for table, col, decl in missing:
                if col not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.db.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))
            self.db.execute("CREATE INDEX IF NOT EXISTS facts_owner_slot_order "
                            "ON facts (owner, slot, COALESCE(at, ''), id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS facts_kind_at ON facts (kind, at DESC, id DESC)")
            self.db.execute("CREATE INDEX IF NOT EXISTS facts_owner_hash ON facts (owner, statement_hash)")
            # Keyset batches bound legacy backfill memory even for a very large
            # profile. Finish each SELECT before updating its source table;
            # mutating a live SQLite cursor's result can skip or repeat rows.
            rows = self.db.execute("SELECT id, statement FROM facts WHERE statement_hash IS NULL "
                                   "ORDER BY id LIMIT 256").fetchall()
            while rows:
                self.db.executemany("UPDATE facts SET statement_hash=? WHERE id=?",
                                    [(fact_hash(r[1]), r[0]) for r in rows])
                rows = self.db.execute(
                    "SELECT id, statement FROM facts WHERE statement_hash IS NULL AND id>? "
                    "ORDER BY id LIMIT 256", (rows[-1][0],)).fetchall()
            self.db.execute("CREATE INDEX IF NOT EXISTS facts_turn ON facts (turn)")
            self.db.execute("CREATE INDEX IF NOT EXISTS turns_expires ON turns (expires) WHERE expires IS NOT NULL")
            self.db.execute("CREATE INDEX IF NOT EXISTS sessions_seq ON sessions (seq)")
            # A persisted generation is comparable across connections. SQLite's
            # data_version and total_changes are local to each connection.
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('units_identity', ?)", (os.urandom(16).hex(),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('units_revision', '0')")
            for operation in ("INSERT", "UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS units_revision_{operation.lower()} "  # nosec B608 - fixed operations
                                f"AFTER {operation} ON units BEGIN UPDATE meta SET value=CAST(value AS INTEGER)+1 "
                                "WHERE key='units_revision'; END")
            self.db.execute("INSERT OR IGNORE INTO fact_sources SELECT id, turn FROM facts")
            self.db.execute("UPDATE facts SET owner=COALESCE((SELECT LOWER(speaker) FROM turns "
                            "WHERE id=facts.turn), '') WHERE owner=''")
            self.db.execute("CREATE TRIGGER IF NOT EXISTS remove_derived_facts BEFORE DELETE ON turns BEGIN "
                            "DELETE FROM facts WHERE id IN (SELECT fact FROM fact_sources WHERE turn=old.id); END")
            if FTS5:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(statement, "
                                "content='facts', content_rowid='id', tokenize='porter unicode61')")
                self.db.execute("INSERT INTO facts_fts(facts_fts) VALUES ('rebuild')")
                # Keep the derived index in the same transaction as its evidence.
                self.db.execute("CREATE TRIGGER IF NOT EXISTS facts_fts_insert AFTER INSERT ON facts BEGIN "
                                "INSERT INTO facts_fts(rowid, statement) VALUES (new.id, new.statement); END")
                self.db.execute("CREATE TRIGGER IF NOT EXISTS facts_fts_delete AFTER DELETE ON facts BEGIN "
                                "INSERT INTO facts_fts(facts_fts, rowid, statement) "
                                "VALUES ('delete', old.id, old.statement); END")
                self.db.execute("CREATE TRIGGER IF NOT EXISTS facts_fts_update "
                                "AFTER UPDATE OF statement ON facts BEGIN "
                                "INSERT INTO facts_fts(facts_fts, rowid, statement) "
                                "VALUES ('delete', old.id, old.statement); "
                                "INSERT INTO facts_fts(rowid, statement) VALUES (new.id, new.statement); END")
            self._reinstate()
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('belief_chains', ?)", (str(SCHEMA_VERSION),))

    def read_stamp(self) -> tuple[int, int]:
        """Detect local writes and commits by other connections, including deletions."""
        stamp = (self.db.total_changes, self.db.execute("PRAGMA data_version").fetchone()[0])
        if stamp != self._read_stamp:
            self._turn_cache.clear()
            self._turn_cache_bytes = 0
            self._read_stamp = stamp
        return stamp

    def unit_stamp(self) -> tuple:
        """Dense index generation, changing only when retrieval units change."""
        return (self.get_meta("units_revision"),) if self._units_identity else self.read_stamp()

    @contextlib.contextmanager
    def read_snapshot(self):
        """Keep all reads in one recall on the same committed SQLite snapshot."""
        with self._lock:
            if self.db.in_transaction:
                yield
                return
            self.db.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def close(self) -> None:
        from commontrace.conversation import embed, search

        with self._lock:
            embed.release_store(self)
            search.forget_store(self)
            for embedder in self._embedders.values():
                embedder.close()
            self._embedders.clear()
            self._turn_cache.clear()
            self._turn_cache_bytes = 0
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- writing -----------------------------------------------------------------

    def add(self, session: str, messages: Iterable[Mapping], *, session_at=None,
            user_speakers: Iterable[str] = (), extract_profile: bool = True) -> dict:
        """Append messages to a session. Re-adding a message already stored is a no-op."""
        if not SESSION_RE.match(session or ""):
            raise ConversationError("session id must be 1-200 printable characters")
        _validate_session_metadata(session)
        started = _moment(session_at)
        users = {_safe_label(str(s).strip())[0].lower() for s in user_speakers}
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
                raw_role = str(message.get("role") or "").strip().lower()
                role, role_redacted = _safe_label(raw_role)
                role = role.lower()
                raw_speaker = str(message.get("speaker") or message.get("name") or raw_role or "user").strip()
                speaker, speaker_redacted = _safe_label(raw_speaker)
                redacted += role_redacted + speaker_redacted
                if not role:
                    role = "user" if not users or speaker.lower() in users else "other"
                at = _moment(message.get("at") or message.get("timestamp")) or started
                expires = _moment(message.get("expires"))
                ref = message.get("id")
                key_ref = str(ref)[:200] if ref not in (None, "") else None
                if ref not in (None, ""):
                    ref, ref_redacted = _safe_label(str(ref), limit=200)
                    redacted += ref_redacted
                else:
                    ref = None
                key = _hash("\x1f".join([session, "ref", key_ref]) if key_ref else
                            "\x1f".join([session, raw_speaker[:120], _iso(at) or "", text]))
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
                if role == "user" and extract_profile:
                    for fact in profile.extract(text):
                        self._insert_fact(turn_id, fact.kind, fact.subject, fact.statement, _iso(at),
                                          fact.slot, "rule", speaker)
                idx += 1
                added += 1
            if added:
                self.db.execute("DELETE FROM summaries WHERE session=?", (session,))
        return {"space": self.space, "session": session, "added": added, "skipped": skipped,
                "secrets_redacted": redacted}

    def delete_session(self, session: str) -> int:
        with self._lock, write_txn(self.db):
            chains = self._chains_for_turns("t.session=?", (session,))
            ids = [r[0] for r in self.db.execute(
                "SELECT u.id FROM units u JOIN turns t ON t.id = u.turn WHERE t.session=?", (session,))]
            if FTS5:
                self.db.executemany("DELETE FROM units_fts WHERE rowid=?", [(i,) for i in ids])
            n = self.db.execute("DELETE FROM turns WHERE session=?", (session,)).rowcount
            self.db.execute("DELETE FROM sessions WHERE id=?", (session,))
            self.db.execute("DELETE FROM meta WHERE key=?", (f"extracted:{session}",))
            self._reinstate(chains)
        self._turn_cache.clear()
        return n

    def _chains_for_turns(self, where: str, params: Iterable) -> list[tuple[str, str]]:
        """Beliefs losing any premise, including a source other than their anchor."""
        return [tuple(r) for r in self.db.execute(
            "SELECT DISTINCT f.owner, f.slot FROM fact_sources s JOIN facts f ON f.id=s.fact "
            "WHERE f.slot IS NOT NULL AND s.turn IN (SELECT id FROM turns t WHERE "
            + where + ")", params)]  # nosec B608 - internal bound predicates

    def _reinstate(self, chains: Iterable[tuple[str, str]] | None = None) -> None:
        """Repair affected chronological beliefs; migration rebuilds every chain.

        A deleted message invalidates its derived facts, but does not change an
        unrelated owner's history. Restricting repair also avoids writing every
        fact (and growing the WAL) when a retention job deletes a small session.
        """
        scope, params = "", ()
        if chains is not None:
            pairs = list(dict.fromkeys(chains))
            if not pairs:
                return
            scope = ("WITH scope AS (SELECT json_extract(value, '$[0]') AS owner, "
                     "json_extract(value, '$[1]') AS slot FROM json_each(?)) ")
            params = (json.dumps(pairs),)
        join = " JOIN scope ON scope.owner=f.owner AND scope.slot=f.slot" if scope else ""
        # Stage the complete window before modifying facts, without copying the
        # history into Python. Its primary key also guarantees indexed lookups
        # on older SQLite versions that scan a repeatedly referenced window CTE.
        self.db.execute("CREATE TEMP TABLE commontrace_belief_successors "
                        "(id INTEGER PRIMARY KEY, successor INTEGER)")
        try:
            self.db.execute(
                scope + "INSERT INTO commontrace_belief_successors "  # nosec B608 - fixed scope query
                "SELECT f.id, LEAD(f.id) OVER (PARTITION BY f.owner, f.slot "
                "ORDER BY COALESCE(f.at, ''), f.id) FROM facts f "
                "JOIN turns t ON t.id=f.turn" + join + " WHERE f.slot IS NOT NULL", params)
            self.db.execute(
                "UPDATE facts SET superseded_by=(SELECT successor FROM commontrace_belief_successors "
                "WHERE id=facts.id) WHERE id IN (SELECT id FROM commontrace_belief_successors) "
                "AND superseded_by IS NOT (SELECT successor FROM commontrace_belief_successors WHERE id=facts.id)")
        finally:
            self.db.execute("DROP TABLE commontrace_belief_successors")

    def _insert_fact(self, turn: int, kind: str, subject: str, statement: str, at: str | None,
                     slot: str | None, source: str, owner: str | None = None) -> int:
        owner = (owner or self.db.execute("SELECT speaker FROM turns WHERE id=?", (turn,)).fetchone()[0]) \
            .strip().lower()
        predecessor = None
        successor_id = None
        if slot:
            # Read before insertion: equal timestamps belong before the new id.
            # A maintained chain already records the predecessor's successor,
            # avoiding a second index seek for append and middle insertions.
            predecessor = self.db.execute(
                "SELECT id, superseded_by FROM facts WHERE slot=? AND owner=? "
                "AND COALESCE(at, '') <= ? ORDER BY COALESCE(at, '') DESC, id DESC LIMIT 1",
                (slot, owner, at or "")).fetchone()
            if predecessor:
                successor_id = predecessor["superseded_by"]
            else:
                successor = self.db.execute(
                    "SELECT id FROM facts WHERE slot=? AND owner=? "
                    "AND COALESCE(at, '') > ? ORDER BY COALESCE(at, ''), id LIMIT 1",
                    (slot, owner, at or "")).fetchone()
                successor_id = successor[0] if successor else None
        fid = self.db.execute(
            "INSERT INTO facts (turn, kind, subject, statement, at, slot, source, owner, statement_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (turn, kind, subject, statement, at, slot, source, owner, fact_hash(statement))).lastrowid
        self.db.execute("INSERT INTO fact_sources VALUES (?, ?)", (fid, turn))
        if slot:
            if successor_id is not None:
                self.db.execute("UPDATE facts SET superseded_by=? WHERE id=?", (successor_id, fid))
            if predecessor:
                self.db.execute("UPDATE facts SET superseded_by=? WHERE id=?", (fid, predecessor[0]))
        return fid

    def add_memories(self, session: str, memories: Iterable[Mapping], *, source: str,
                     extracted_through: int | None = None,
                     expected_sources: Mapping[int, str] | None = None) -> int:
        """Store derived memories with optional owner and exact source_turn_ids.

        Omitted sources default to the latest user turn. Deleting any supporting
        message also deletes the derived memory, retaining no unsupported fact.
        Model extraction can supply expected_sources to validate the exact read
        evidence before publishing either derived memories or its checkpoint.
        """
        added = 0
        with self._lock, write_txn(self.db):
            if expected_sources is not None:
                if not isinstance(expected_sources, Mapping) or not expected_sources or any(
                        not isinstance(tid, int) or isinstance(tid, bool) or not isinstance(proof, str)
                        for tid, proof in expected_sources.items()):
                    raise ConversationError("expected_sources must map turn ids to evidence hashes")
                rows = self.db.execute(
                    "SELECT * FROM turns WHERE session=? AND id IN (SELECT value FROM json_each(?))",
                    (session, json.dumps(list(expected_sources)))).fetchall()
                if len(rows) != len(expected_sources) or any(
                        self._row_turn(r).evidence_hash() != expected_sources[r["id"]] for r in rows):
                    raise ConversationError("source evidence changed during extraction; retry")
            if extracted_through is not None:
                checkpoint = f"extracted:{session}"
                done = self.get_meta(checkpoint)
                if done is not None and int(done) >= extracted_through:
                    return 0
            row = self.db.execute("SELECT id, at, speaker FROM turns WHERE session=? ORDER BY idx DESC LIMIT 1",
                                  (session,)).fetchone()
            if row is None:
                raise ConversationError(f"no session {session!r} in space {self.space!r}")
            # A profile extracted from a user/assistant session belongs to the
            # user, even when the last message was the assistant's reply.
            row = self.db.execute("SELECT id, at, speaker FROM turns WHERE session=? AND role='user' "
                                  "ORDER BY idx DESC LIMIT 1", (session,)).fetchone() or row
            for m in memories:
                raw = str(m.get("text") or "").strip()[:profile.MAX_STATEMENT]
                text, _found = memory_guard.redact_secrets(raw)
                pii_text, _pii = memory_guard.redact_pii(text)
                text = pii_text
                kind = str(m.get("kind") or "fact").strip().lower()
                if not text or kind not in FACT_KINDS:
                    continue
                source_ids = m.get("source_turn_ids")
                if source_ids is not None:
                    if not isinstance(source_ids, (list, tuple)) or not source_ids \
                            or any(not isinstance(tid, int) or isinstance(tid, bool) for tid in source_ids):
                        raise ConversationError("source_turn_ids must be a non-empty list of turn ids")
                    source_ids = list(dict.fromkeys(source_ids))
                    sources = self.db.execute(
                        "SELECT id, at FROM turns WHERE session=? "
                        "AND id IN (SELECT value FROM json_each(?)) ORDER BY COALESCE(at, ''), idx",
                        (session, json.dumps(source_ids))).fetchall()
                    if len(sources) != len(source_ids):
                        raise ConversationError("every source turn must exist in the memory's session")
                    anchor = sources[-1]
                else:
                    source_ids, anchor = [row["id"]], row
                slot = str(m["slot"]).strip().lower()[:80] if m.get("slot") else None
                owner = _safe_label(str(m.get("owner") or row["speaker"]).strip())[0].lower()
                # Deduplicate within an owner's live beliefs. A repeated old
                # statement after an update is a reversion, not a duplicate.
                if self.db.execute(
                        "SELECT 1 FROM facts WHERE owner=? AND statement_hash=? "
                        "AND (? IS NULL OR slot IS NULL OR superseded_by IS NULL) LIMIT 1",
                        (owner, fact_hash(text), slot)).fetchone():
                    continue
                fid = self._insert_fact(anchor["id"], kind, profile.subject_of(text), text,
                                        _iso(_moment(m.get("at"))) if m.get("at") else anchor["at"], slot, source,
                                        owner)
                self.db.execute("DELETE FROM fact_sources WHERE fact=?", (fid,))
                self.db.executemany("INSERT INTO fact_sources VALUES (?, ?)", [(fid, tid) for tid in source_ids])
                added += 1
            if extracted_through is not None:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                (checkpoint, str(extracted_through)))
        return added

    def set_summary(self, session: str, text: str, method: str, *, expected_revision=None) -> None:
        text, _ = memory_guard.redact_secrets(text)
        text, _ = memory_guard.redact_pii(text)
        with self._lock, write_txn(self.db):
            if expected_revision is not None and self.unit_stamp() != expected_revision:
                raise ConversationError("session evidence changed during summarization; retry")
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
            chains = self._chains_for_turns("(" + where + ")", args)
            affected_sessions = [r[0] for r in self.db.execute(
                "SELECT DISTINCT session FROM turns WHERE " + where, args)]  # nosec B608 - fixed clauses
            ids = [r[0] for r in self.db.execute(
                f"SELECT u.id FROM units u JOIN turns t ON t.id = u.turn WHERE {where}", args)]  # nosec B608
            if FTS5:
                self.db.executemany("DELETE FROM units_fts WHERE rowid=?", [(i,) for i in ids])
            n = self.db.execute(f"DELETE FROM turns WHERE {where}", args).rowcount  # nosec B608
            self.db.execute("DELETE FROM meta WHERE key IN (SELECT 'extracted:' || id FROM sessions "
                            "WHERE id NOT IN (SELECT DISTINCT session FROM turns))")
            # Purging a session's tail allows later appends to reuse its turn
            # indices. Rewind only past the surviving high-water mark, so the
            # next extraction cannot skip those newly appended messages.
            self.db.executemany(
                "UPDATE meta SET value=CAST(MIN(CAST(value AS INTEGER), "
                "(SELECT MAX(idx) FROM turns WHERE session=?)) AS TEXT) WHERE key=? "
                "AND CAST(value AS INTEGER)>(SELECT MAX(idx) FROM turns WHERE session=?)",
                [(session, f"extracted:{session}", session) for session in affected_sessions])
            self.db.execute("DELETE FROM sessions WHERE id NOT IN (SELECT DISTINCT session FROM turns)")
            self._reinstate(chains)
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
        self.read_stamp()
        ids = list(dict.fromkeys(ids))
        result = {i: self._turn_cache[i] for i in ids if i in self._turn_cache}
        missing = [i for i in ids if i not in self._turn_cache]
        if missing:
            for r in self.db.execute("SELECT * FROM turns WHERE id IN (SELECT value FROM json_each(?))",
                                     (json.dumps(missing),)):
                result[r["id"]] = self._row_turn(r)
        for i in ids:
            if i in result:
                if i not in self._turn_cache:
                    self._turn_cache_bytes += self._turn_size(result[i])
                self._turn_cache[i] = result[i]
                self._turn_cache.move_to_end(i)
                while len(self._turn_cache) > TURN_CACHE_SIZE or self._turn_cache_bytes > TURN_CACHE_BYTES:
                    _, removed = self._turn_cache.popitem(last=False)
                    self._turn_cache_bytes -= self._turn_size(removed)
        return {i: result[i] for i in ids if i in result}

    @staticmethod
    def _turn_size(turn: Turn) -> int:
        return sys.getsizeof(turn.text) + 512 + len(turn.dates) * 128

    def neighbours(self, turn: Turn, before: int, after: int) -> list[int]:
        return [r[0] for r in self.db.execute(
            "SELECT id FROM turns WHERE session=? AND idx BETWEEN ? AND ? AND idx != ? ORDER BY idx",
            (turn.session, turn.idx - before, turn.idx + after, turn.idx))]

    def units(self, *, after_id: int | None = None, limit: int | None = None) -> list[tuple[int, int, str, str]]:
        """Retrieval passages, optionally bounded by a stable unit-ID cursor.

        The no-argument form retains the complete-corpus compatibility API;
        index builders should prefer :meth:`unit_batches` to stream bodies.
        """
        query, params = "SELECT id, turn, body, hash FROM units", []
        if after_id is not None:
            query += " WHERE id>?"
            params.append(after_id)
        query += " ORDER BY id"
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("unit limit must be a nonnegative integer")
            query += " LIMIT ?"
            params.append(limit)
        return [tuple(r) for r in self.db.execute(query, params)]

    def unit_batches(self, size: int = 1024, *, allowed: set[int] | None = None):
        """Stream eligible passages without materialising the interaction log."""
        if size <= 0:
            raise ValueError("batch size must be positive")
        query = "SELECT id, turn, body, hash FROM units"
        params = ()
        if allowed is not None:
            query += " WHERE turn IN (SELECT value FROM json_each(?))"
            params = (json.dumps(sorted(allowed)),)
        cursor = self.db.execute(query + " ORDER BY id", params)
        while rows := cursor.fetchmany(size):
            yield [tuple(r) for r in rows]

    def unit_turns(self, unit_ids: Iterable[int]) -> dict[int, int]:
        ids = list(unit_ids)
        if not ids:
            return {}
        return dict(self.db.execute("SELECT id, turn FROM units WHERE id IN (SELECT value FROM json_each(?))",
                                    (json.dumps(ids),)).fetchall())

    def lexical(self, query: str, limit: int, *, allowed: set[int] | None = None) -> list[tuple[int, float]]:
        """(unit id, score) by BM25, best first.

        Scores are sigmoid-normalized to [0, 1] (see :func:`sigmoid_bm25`,
        adapted from Mem0's ``normalize_bm25``) so they share a scale with
        cosine similarity instead of living on an unbounded 0–20+ range.
        """
        match = _fts_query(query)
        if not match or limit <= 0 or allowed == set():
            return []
        n_terms = len(match.split(" OR "))
        if FTS5:
            eligible = (" AND rowid IN (SELECT id FROM units WHERE turn IN "
                        "(SELECT value FROM json_each(?)))") if allowed is not None else ""
            params = (match, json.dumps(sorted(allowed)), limit) if allowed is not None else (match, limit)
            rows = self.db.execute(
                "SELECT rowid, bm25(units_fts) FROM units_fts WHERE units_fts MATCH ? "  # nosec B608 - fixed eligibility SQL
                + eligible + " ORDER BY bm25(units_fts) LIMIT ?", params)
            return [(r[0], sigmoid_bm25(-r[1], n_terms)) for r in rows]
        units = (unit for batch in self.unit_batches(allowed=allowed) for unit in batch)
        return _python_bm25(units, query, limit)

    def in_window(self, lo: dt.date, hi: dt.date) -> set[int]:
        """Turns said within [lo, hi], or whose grounded dates overlap it."""
        lo_s, hi_s = lo.isoformat(), (hi + dt.timedelta(days=1)).isoformat()
        return {r[0] for r in self.db.execute(
            "SELECT id FROM turns WHERE (at >= ? AND at < ?) OR (lo IS NOT NULL AND lo < ? AND hi >= ?)",
            (lo_s, hi_s, hi_s, lo.isoformat()))}

    def _fact_conditions(self, kinds: Iterable[str] = (), *, history: bool = False,
                         as_of=None, allowed: set[int] | None = None,
                         candidates: Iterable[int] | None = None) -> tuple[str, list]:
        """Shared evidence and belief eligibility for profile reads and candidate search."""
        kinds = list(kinds)
        moment = _iso(_moment(as_of)) if as_of is not None else None
        clauses, params = [], []

        def eligible(f, t):
            conditions, values = [], []
            if moment is not None:
                conditions.append(f"({f}.at IS NULL OR {f}.at <= ?)")
                values.append(moment)
                conditions.append(f"({t}.at IS NULL OR {t}.at <= ?)")
                values.append(moment)
            if allowed is not None:
                conditions.append(f"{t}.id IN (SELECT value FROM json_each(?))")  # nosec B608 - fixed alias, bound ids
                values.append(json.dumps(sorted(allowed)))
            excluded, source_values = [], []
            if moment is not None:
                excluded.append("st.at > ?")
                source_values.append(moment)
            if allowed is not None:
                excluded.append("st.id NOT IN (SELECT value FROM json_each(?))")
                source_values.append(json.dumps(sorted(allowed)))
            if excluded and self._has_sources:
                conditions.append(f"NOT EXISTS (SELECT 1 FROM fact_sources fs JOIN turns st ON st.id=fs.turn "  # nosec B608 - fixed aliases
                                  f"WHERE fs.fact={f}.id AND (" + " OR ".join(excluded) + "))")
                values.extend(source_values)
            return conditions, values

        clauses, params = eligible("f", "t")
        if kinds:
            clauses.append("f.kind IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(kinds))
        if candidates is not None:
            clauses.append("f.id IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(list(candidates)))
        if not history and moment is None and allowed is None and "owner" in self._fact_columns:
            clauses.append("f.superseded_by IS NULL")
        elif not history and "slot" in self._fact_columns:
            newer, values = eligible("g", "u")
            same_owner = "g.owner=f.owner" if "owner" in self._fact_columns else "LOWER(u.speaker)=LOWER(t.speaker)"
            clauses.append(
                "(f.slot IS NULL OR NOT EXISTS (SELECT 1 FROM facts g JOIN turns u ON u.id=g.turn "  # nosec B608 - fixed clauses
                "WHERE g.slot=f.slot AND " + same_owner + " "
                "AND (COALESCE(g.at, ''), g.id) > (COALESCE(f.at, ''), f.id)"
                + (" AND " + " AND ".join(newer) if newer else "") + "))")
            params.extend(values)
        return " AND ".join(clauses) or "1", params

    def facts(self, kinds: Iterable[str] = (), *, history: bool = False,
              as_of=None, allowed: set[int] | None = None,
              candidates: Iterable[int] | None = None) -> list[dict]:
        """Current beliefs, oldest first. `history` includes superseded statements;
        `as_of` and `allowed` restrict every supporting source before belief selection."""
        where, params = self._fact_conditions(kinds, history=history, as_of=as_of,
                                             allowed=allowed, candidates=candidates)
        legacy = "".join(", " + expr + " AS " + name for name, expr in {
            "owner": "LOWER(t.speaker)", "slot": "NULL", "source": "'rule'", "superseded_by": "NULL"
        }.items() if name not in self._fact_columns)
        successor = " LEFT JOIN facts n ON n.id=f.superseded_by" if "superseded_by" in self._fact_columns else ""
        validity = "n.at" if successor else "NULL"
        return [dict(r) for r in self.db.execute(
            "SELECT f.*, t.session, t.speaker, " + validity + " AS valid_until" + legacy + " FROM facts f "  # nosec B608 - fixed schema expressions
            "JOIN turns t ON t.id=f.turn" + successor + " WHERE "
            + where + " ORDER BY f.at, f.id", params)]

    def recall_facts(self, query: str, *, as_of=None, allowed: set[int] | None = None,
                     instructions: bool = True, profile_facts: bool = True, limit: int = 128) -> list[dict]:
        """Bounded profile candidates from a separate sparse index and standing rules.

        Recall does not load the entire profile on every question. The normal
        `facts` method remains the exhaustive, inspectable history interface.
        """
        match = _fts_query(query)
        if not FTS5 or not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='facts_fts'").fetchone():
            return self.facts(as_of=as_of, allowed=allowed)
        ids: set[int] = set()
        condition, params = self._fact_conditions(as_of=as_of, allowed=allowed)
        suffix = " AND " + condition
        if profile_facts and match:
            ids.update(r[0] for r in self.db.execute(
                "SELECT f.id FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid JOIN turns t ON t.id=f.turn "  # nosec B608 - fixed conditions
                "WHERE facts_fts MATCH ?" + suffix + " ORDER BY bm25(facts_fts) LIMIT ?",
                [match, *params, limit]))
        # Rules about answer format can be unrelated to the query. The kind
        # index supplies them, and a small recent profile supports advice.
        kinds = (["instruction"] if instructions else []) + (
            ["preference", "dislike", "favorite", "identity", "habit"] if profile_facts else [])
        for kind in kinds:
            ids.update(r[0] for r in self.db.execute(
                "SELECT f.id FROM facts f JOIN turns t ON t.id=f.turn WHERE f.kind=?"  # nosec B608 - bound kind and metadata
                + suffix + " ORDER BY f.at DESC, f.id DESC LIMIT ?",
                [kind, *params, limit if kind == "instruction" else 16]))
        return self.facts(as_of=as_of, allowed=allowed, candidates=ids)

    def entity_turns(self, names: Iterable[str], *, max_matches: int | None = None) -> dict[str, list[int]]:
        """The turns mentioning each name or spoken by them.

        One batched query for all names (cognee single-WHERE-IN pattern),
        not one UNION per name.
        """
        names = list(dict.fromkeys(n.lower() for n in names))
        if not names:
            return {}
        if max_matches is not None:
            # A ubiquitous speaker/entity contributes almost no information.
            # Count using indexes instead of materialising its whole history.
            names = [n for n in names if self.db.execute(
                "SELECT (SELECT COUNT(*) FROM (SELECT 1 FROM entities WHERE name=? LIMIT ?)) "
                "+ (SELECT COUNT(*) FROM (SELECT 1 FROM turns WHERE LOWER(speaker)=? LIMIT ?))",
                (n, max_matches + 1, n, max_matches + 1)).fetchone()[0] <= max_matches]
            if not names:
                return {}
        out: dict[str, list[int]] = {name: [] for name in names}
        placeholders = ",".join("?" for _ in names)
        for found, turn in self.db.execute(
                f"SELECT name, turn FROM entities WHERE name IN ({placeholders})",  # nosec B608 - placeholders only
                names):
            out[found].append(turn)
        seen = {n: set(turns) for n, turns in out.items()}
        for speaker, turn in self.db.execute(
                f"SELECT LOWER(speaker), id FROM turns WHERE LOWER(speaker) IN ({placeholders})",  # nosec B608
                names):
            if turn not in seen[speaker]:
                out[speaker].append(turn)
                seen[speaker].add(turn)
        return out

    def summaries(self) -> dict[str, dict]:
        return {r["session"]: dict(r) for r in self.db.execute(
            "SELECT s.* FROM summaries s WHERE s.turns=(SELECT COUNT(*) FROM turns t WHERE t.session=s.session)")}

    def fact_evidence(self, fact_id: int) -> list[dict]:
        """The exact stored source messages supporting a derived memory."""
        if not self._has_sources:
            return [dict(r) for r in self.db.execute(
                "SELECT t.id, t.ref, t.session, t.at, t.speaker, t.role, t.text FROM facts f "
                "JOIN turns t ON t.id=f.turn WHERE f.id=?", (fact_id,))]
        return [dict(r) for r in self.db.execute(
            "SELECT t.id, t.ref, t.session, t.at, t.speaker, t.role, t.text FROM fact_sources s "
            "JOIN turns t ON t.id=s.turn WHERE s.fact=? ORDER BY COALESCE(t.at, ''), t.id", (fact_id,))]

    def fact_source_ids(self, fact_ids: Iterable[int]) -> dict[int, list[int]]:
        ids = list(fact_ids)
        table, fact, turn = ("fact_sources", "fact", "turn") if self._has_sources else ("facts", "id", "turn")
        out: dict[int, list[int]] = {}
        for fid, tid in self.db.execute(
                f"SELECT {fact}, {turn} FROM {table} WHERE {fact} IN (SELECT value FROM json_each(?))",  # nosec B608 - fixed schema names
                (json.dumps(ids),)):
            out.setdefault(fid, []).append(tid)
        return out

    def timeline(self, limit: int = 500, *, after_seq: int | None = None) -> list[dict]:
        """Chronological sessions, with optional keyset pagination by ``seq``.

        Page limits never change the chronology or discard durable evidence.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("timeline limit must be a nonnegative integer")
        query = """
        SELECT s.id as session, s.started_at, s.seq, sm.text as summary, COUNT(t.id) as turns,
               MIN(t.id) as first_turn, MAX(t.id) as last_turn
        FROM sessions s
        LEFT JOIN summaries sm ON sm.session = s.id
            AND sm.turns=(SELECT COUNT(*) FROM turns st WHERE st.session=s.id)
        LEFT JOIN turns t ON t.session = s.id
        WHERE (? IS NULL OR s.seq > ?)
        GROUP BY s.id
        ORDER BY s.seq ASC
        LIMIT ?
        """
        rows = self.db.execute(query, (after_seq, after_seq, limit)).fetchall()
        return [dict(r) for r in rows]

    def session_turns(self, session: str, *, after_idx: int = -1,
                      through_idx: int | None = None, limit: int | None = None) -> list[Turn]:
        """Ordered source messages, optionally a bounded checkpoint batch."""
        query, params = "SELECT * FROM turns WHERE session=? AND idx>?", [session, after_idx]
        if through_idx is not None:
            query += " AND idx<=?"
            params.append(through_idx)
        query += " ORDER BY idx"
        if limit is not None:
            if limit < 0:
                raise ValueError("turn limit must be non-negative")
            query += " LIMIT ?"
            params.append(limit)
        rows = self.db.execute(query, params).fetchall()
        return [self._row_turn(r) for r in rows]

    def allowed(self, *, sessions: Iterable[str] = (), speakers: Iterable[str] = (), since=None, until=None,
                now: dt.datetime | None = None) -> set[int] | None:
        """The turns a filtered recall may use, or None when nothing is filtered out."""
        sessions, speakers = list(sessions), [_safe_label(str(s).strip())[0].lower() for s in speakers]
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
                                  (moment,)).fetchone() if self._has_expiry else None
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
        """Versioned JSONL sessions including beliefs, provenance and checkpoints."""
        from commontrace.conversation.portable import export

        yield from export(self)

    def import_sessions(self, rows: Iterable[Mapping]) -> dict:
        """Atomically merge exported sessions; legacy message-only exports remain readable."""
        from commontrace.conversation.portable import restore

        return restore(self, rows)


def _python_bm25(units, query: str, limit: int) -> list[tuple[int, float]]:
    def terms(text):
        return (stem(m.group()) for m in _BM25_WORD_RE.finditer(text.lower())
                if m.group() not in profile.STOPWORDS)

    q = set(terms(query))
    if not q or limit <= 0:
        return []
    # Keep only query-term frequencies and document lengths. Source bodies are
    # streamed by the store, rather than copied into a second full corpus.
    docs, df, total_length = [], Counter(), 0
    for uid, _turn, body, _h in units:
        counts, length = Counter(), 0
        for term in terms(body):
            length += 1
            if term in q:
                counts[term] += 1
        docs.append((uid, length, counts))
        df.update(counts.keys())
        total_length += length
    if not docs:
        return []
    avg = total_length / len(docs)
    idfs = {t: math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5)) for t in q}

    def scored():
        for uid, length, counts in docs:
            score = 0.0
            # Preserve query-set iteration order so floating sums and tie order
            # exactly match the original fallback on every supported scorer.
            for t in q:
                tf = counts[t]
                if tf:
                    score += idfs[t] * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
            if score > 0:
                yield uid, sigmoid_bm25(score, len(q))

    return heapq.nlargest(limit, scored(), key=lambda x: x[1])
