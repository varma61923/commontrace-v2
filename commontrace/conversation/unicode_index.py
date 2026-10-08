"""Derived Unicode postings; canonical units and their vector journal stay intact.

ASCII queries retain the existing porter/BM25 path. Non-ASCII word terms use
exact lowercased words and CJK bigrams (plus unigrams for one-character queries).
SQL triggers invalidate postings even for writers outside the Python API. Frozen
or concurrently dirtied snapshots use the same scorer over streamed source units.
"""
from __future__ import annotations

import heapq
import json
import math
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable

from commontrace._lexical import WORD_RE, has_cjk, segment_cjk

VERSION = "1"
MAX_QUERY_TERMS = 256
MAX_QUERY_CHARS = 16384


def terms(text: str) -> list[str]:
    """Unicode words and CJK n-grams; ASCII words belong to the legacy arm."""
    out: list[str] = []
    for word in WORD_RE.findall(unicodedata.normalize("NFC", text).lower()):
        if word.isascii():
            continue
        out.extend(part for part in segment_cjk(word) if not part.isascii())
        # Single-character searches need postings even within longer CJK runs.
        if has_cjk(word):
            out.extend(char for char in word if has_cjk(char))
    return out


def relevant(text: str) -> bool:
    return not text.isascii() and any(ord(char) > 127 and char.isalnum()
                                     for char in unicodedata.normalize("NFC", text))


def ascii_query(text: str) -> str:
    """Keep complete ASCII words, never fragments inside accented/CJK words."""
    return " ".join(word for word in WORD_RE.findall(unicodedata.normalize("NFC", text))
                    if word.isascii())


def query_terms(text: str) -> list[str]:
    """Bounded word/bigram queries; unigrams only for actual singleton runs."""
    found: dict[str, None] = {}
    for word in WORD_RE.findall(unicodedata.normalize("NFC", text[:MAX_QUERY_CHARS]).lower()):
        for part in segment_cjk(word):
            if not part.isascii():
                found[part] = None
                if len(found) == MAX_QUERY_TERMS:
                    return list(found)
    return list(found)


def installed(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM meta WHERE key='unicode_postings'").fetchone()
    return row is not None and row[0] == VERSION


def initialize(db: sqlite3.Connection) -> None:
    """Install once inside the caller's write transaction, without source edits."""
    if installed(db):
        return
    db.execute("CREATE TABLE IF NOT EXISTS unicode_documents "
               "(unit INTEGER PRIMARY KEY, length INTEGER NOT NULL CHECK(length>0))")
    db.execute("CREATE TABLE IF NOT EXISTS unicode_postings "
               "(term TEXT NOT NULL, unit INTEGER NOT NULL, frequency INTEGER NOT NULL, "
               "PRIMARY KEY(term,unit)) WITHOUT ROWID")
    db.execute("CREATE INDEX IF NOT EXISTS unicode_postings_unit ON unicode_postings(unit)")
    db.execute("CREATE TABLE IF NOT EXISTS unicode_dirty (unit INTEGER PRIMARY KEY)")
    db.execute("CREATE TRIGGER IF NOT EXISTS unicode_units_insert AFTER INSERT ON units "
               "WHEN length(CAST(new.body AS BLOB))<>length(new.body) BEGIN "
               "INSERT OR IGNORE INTO unicode_dirty VALUES(new.id); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS unicode_units_update AFTER UPDATE ON units BEGIN "
               "DELETE FROM unicode_postings WHERE unit=old.id; "
               "DELETE FROM unicode_documents WHERE unit=old.id; "
               "DELETE FROM unicode_dirty WHERE unit=old.id; "
               "INSERT OR IGNORE INTO unicode_dirty SELECT new.id "
               "WHERE length(CAST(new.body AS BLOB))<>length(new.body); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS unicode_units_delete AFTER DELETE ON units BEGIN "
               "DELETE FROM unicode_postings WHERE unit=old.id; "
               "DELETE FROM unicode_documents WHERE unit=old.id; "
               "DELETE FROM unicode_dirty WHERE unit=old.id; END")
    db.execute("INSERT OR IGNORE INTO unicode_dirty SELECT id FROM units "
               "WHERE length(CAST(body AS BLOB))<>length(body)")
    db.execute("INSERT OR REPLACE INTO meta VALUES('unicode_postings', ?)", (VERSION,))


def dirty(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM unicode_dirty LIMIT 1").fetchone() is not None


def refresh(db: sqlite3.Connection) -> None:
    """Bounded keyset batches; caller owns the write transaction and store lock."""
    while True:
        rows = db.execute("SELECT d.unit,u.body FROM unicode_dirty d LEFT JOIN units u "
                          "ON u.id=d.unit ORDER BY d.unit LIMIT 256").fetchall()
        if not rows:
            return
        for uid, body in rows:
            db.execute("DELETE FROM unicode_postings WHERE unit=?", (uid,))
            db.execute("DELETE FROM unicode_documents WHERE unit=?", (uid,))
            counts = Counter(terms(body)) if body is not None else Counter()
            if counts:
                db.execute("INSERT INTO unicode_documents VALUES(?,?)", (uid, sum(counts.values())))
                db.executemany("INSERT INTO unicode_postings VALUES(?,?,?)",
                               ((term, uid, count) for term, count in counts.items()))
            db.execute("DELETE FROM unicode_dirty WHERE unit=?", (uid,))


def _score(frequencies: dict[str, int], length: int, average: float,
           idfs: dict[str, float]) -> float:
    raw = sum(idfs[term] * count * 2.2 /
              (count + 1.2 * (0.25 + 0.75 * length / average))
              for term, count in frequencies.items())
    n = len(idfs)
    midpoint, steepness = ((5.0, 0.7) if n <= 3 else (7.0, 0.6) if n <= 6 else
                          (9.0, 0.5) if n <= 9 else (10.0, 0.5) if n <= 15 else (12.0, 0.5))
    return 1.0 / (1.0 + math.exp(-steepness * (raw - midpoint)))


def rank(db: sqlite3.Connection, query: str, limit: int,
         allowed: set[int] | None) -> list[tuple[int, float]]:
    """BM25 over eligible Unicode-bearing units; postings never cross a scope."""
    wanted = query_terms(query)
    if not wanted or limit <= 0 or allowed == set():
        return []
    scope = "u.turn IN (SELECT value FROM json_each(?))" if allowed is not None else "1"
    scope_params: tuple[str, ...] = (json.dumps(sorted(allowed)),) if allowed is not None else ()
    size, average = db.execute(  # nosec B608 - fixed eligibility SQL, bound scope
        "SELECT COUNT(*),AVG(d.length) FROM unicode_documents d JOIN units u ON u.id=d.unit WHERE "
        + scope, scope_params).fetchone()
    if not size:
        return []
    # One indexed seek per query term. Scope is applied before statistics and
    # limiting, so hidden tenants cannot crowd out or reweight visible evidence.
    idfs: dict[str, float] = {}
    query_json = json.dumps(wanted)
    dfs = dict(db.execute(  # nosec B608 - fixed eligibility SQL, bound terms and scope
        "SELECT p.term,COUNT(*) FROM unicode_postings p JOIN units u ON u.id=p.unit "
        "WHERE p.term IN (SELECT value FROM json_each(?)) AND " + scope + " GROUP BY p.term",
        (query_json, *scope_params)))
    for term in wanted:
        df = dfs.get(term, 0)
        idfs[term] = math.log(1 + (size - df + 0.5) / (df + 0.5))
    rows = db.execute(  # nosec B608 - fixed eligibility SQL, bound terms and scope
        "SELECT p.unit,d.length,p.term,p.frequency FROM unicode_postings p "
        "JOIN unicode_documents d ON d.unit=p.unit JOIN units u ON u.id=p.unit "
        "WHERE p.term IN (SELECT value FROM json_each(?)) AND " + scope + " ORDER BY p.unit,p.term",
        (query_json, *scope_params))

    def scores() -> Iterable[tuple[int, float]]:
        previous: int | None = None
        length = 0
        counts: dict[str, int] = {}
        for uid, length_row, term, frequency in rows:
            if previous is not None and uid != previous:
                yield previous, _score(counts, length, average, idfs)
                counts = {}
            previous, length = uid, length_row
            counts[term] = frequency
        if previous is not None:
            yield previous, _score(counts, length, average, idfs)

    return heapq.nsmallest(limit, scores(), key=lambda hit: (-hit[1], hit[0]))


def stream_rank(units: Iterable[tuple[int, int, str, str]], query: str,
                limit: int) -> list[tuple[int, float]]:
    """Exact scoring fallback for frozen legacy banks and dirty read snapshots."""
    wanted = set(query_terms(query))
    if not wanted or limit <= 0:
        return []
    size, total = 0, 0
    dfs: Counter[str] = Counter()
    candidates: list[tuple[int, int, dict[str, int]]] = []
    for uid, _turn, body, _hash in units:
        counts = Counter(terms(body))
        if not counts:
            continue
        length = sum(counts.values())
        size += 1
        total += length
        matches = {term: counts[term] for term in sorted(wanted) if term in counts}
        if matches:
            candidates.append((uid, length, matches))
            dfs.update(matches.keys())
    if not size:
        return []
    idfs = {term: math.log(1 + (size - dfs[term] + 0.5) / (dfs[term] + 0.5)) for term in wanted}
    return heapq.nsmallest(limit, ((uid, _score(counts, length, total / size, idfs))
                                 for uid, length, counts in candidates),
                          key=lambda hit: (-hit[1], hit[0]))
