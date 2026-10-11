"""Explicit SQLite WAL migration with an authoritative event ledger.

Facts, dedup postings, contradiction slots and FTS5 are projections. Existing
JSONL stores remain unchanged until migrated. Each operation owns a coherent
SQLite transaction and fresh fact map. A bounded pool retains idle connections,
never fact objects or active transactions.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from commontrace.hierarchical import AtomicFact

from commontrace import _jsonl, paths
from commontrace.memory_record import MemoryRecord
from commontrace.sqlite_pool import Connection as _Connection
from commontrace.sqlite_pool import ConnectionPool

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT NOT NULL,
 version INTEGER NOT NULL, operation TEXT NOT NULL, record TEXT NOT NULL,
 UNIQUE(fact_id, version));
CREATE TABLE IF NOT EXISTS facts (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, norm TEXT NOT NULL,
 block TEXT NOT NULL, slot TEXT NOT NULL, negative INTEGER NOT NULL,
 numbers TEXT NOT NULL, status TEXT NOT NULL, forgotten INTEGER NOT NULL,
 category TEXT NOT NULL, confidence REAL NOT NULL, length INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS fact_norm ON facts(norm, status);
CREATE INDEX IF NOT EXISTS fact_slot ON facts(slot, negative, numbers, status);
CREATE INDEX IF NOT EXISTS fact_block ON facts(block, status);
CREATE TABLE IF NOT EXISTS lexicon (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, term TEXT NOT NULL,
 documents INTEGER NOT NULL DEFAULT 0, UNIQUE(kind,term));
CREATE TABLE IF NOT EXISTS postings (
 term_id INTEGER NOT NULL, fact_row INTEGER NOT NULL, count INTEGER NOT NULL,
 PRIMARY KEY(term_id,fact_row)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS posting_fact ON postings(fact_row);
CREATE VIEW IF NOT EXISTS terms AS
 SELECT l.kind,l.term,f.id AS fact_id,p.count FROM postings p
 JOIN lexicon l ON l.id=p.term_id JOIN facts f ON f.rowid=p.fact_row;
CREATE VIEW IF NOT EXISTS term_counts AS SELECT kind,term,documents FROM lexicon;
CREATE TRIGGER IF NOT EXISTS clear_terms INSTEAD OF DELETE ON terms BEGIN
 DELETE FROM postings WHERE term_id=(SELECT id FROM lexicon WHERE kind=old.kind AND term=old.term)
 AND fact_row=(SELECT rowid FROM facts WHERE id=old.fact_id); END;
CREATE TRIGGER IF NOT EXISTS clear_counts INSTEAD OF DELETE ON term_counts BEGIN
 UPDATE lexicon SET documents=0 WHERE kind=old.kind AND term=old.term; END;
CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(fact_id UNINDEXED, statement);
"""

def close_connections(root: str | None = None) -> None:
    """Drain idle handles before replacing a quiescent store; active owners finish."""
    _CONNECTIONS.close(database_path(root) if root is not None else None)


def _borrow_connection(root: str) -> tuple[tuple[str, int, int], _Connection, int]:
    return _CONNECTIONS.borrow(database_path(root))


def _return_connection(key: tuple[str, int, int], connection: _Connection, generation: int) -> None:
    _CONNECTIONS.release(key, connection, generation)


def database_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "facts", "facts.sqlite3")


def enabled(root: str) -> bool:
    # Only successful migration publishes this path. Temporary databases never
    # select a new backend, even when a migration is interrupted.
    return os.path.isfile(database_path(root))


def _connect(path: str, *, create: bool = False) -> _Connection:
    if create:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    # Exclusive checkout serializes use of each handle across threads.
    connection = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False, factory=_Connection)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA synchronous=FULL")
    try:
        if create:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        legacy = connection.execute("SELECT type FROM sqlite_master WHERE name='terms'").fetchone() == ("table",)
        near_current = connection.execute(
            "SELECT value FROM meta WHERE key='near_postings'"
        ).fetchone() == ("global-v2",)
        if legacy or not near_current:
            # Recheck under the writer lock: restarting processes may see the
            # same old projection. Neither conversion edits authoritative bytes.
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute("SELECT type FROM sqlite_master WHERE name='terms'").fetchone() == ('table',):
                connection.execute('ALTER TABLE terms RENAME TO legacy_terms')
                connection.execute('ALTER TABLE term_counts RENAME TO legacy_counts')
                statement = ''
                for line in _SCHEMA.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ''
                connection.execute("INSERT INTO lexicon(kind,term,documents) "
                                   "SELECT kind,term,documents FROM legacy_counts")
                connection.execute("INSERT INTO postings SELECT l.id,f.rowid,t.count FROM legacy_terms t "
                                   "JOIN lexicon l ON l.kind=t.kind AND l.term=t.term "
                                   "JOIN facts f ON f.id=t.fact_id")
                connection.execute('DROP TABLE legacy_terms')
                connection.execute('DROP TABLE legacy_counts')
            _upgrade_near_projection(connection)
            connection.commit()
    except BaseException:
        connection.rollback()
        connection.close()
        raise
    return connection


def _upgrade_near_projection(connection: sqlite3.Connection) -> None:
    """Replace block-specific term dictionaries in the caller's writer transaction."""
    if connection.execute("SELECT value FROM meta WHERE key='near_postings'").fetchone() == ("global-v2",):
        return
    connection.execute("CREATE INDEX IF NOT EXISTS fact_block ON facts(block,status)")
    connection.execute(
        "INSERT INTO lexicon(kind,term,documents) SELECT 'near-v2',term,SUM(documents) FROM lexicon "
        "WHERE kind>='near:' AND kind<'near;' GROUP BY term "
        "ON CONFLICT(kind,term) DO UPDATE SET documents=documents+excluded.documents"
    )
    connection.execute(
        "INSERT INTO postings SELECT current.id,p.fact_row,p.count FROM postings p "
        "JOIN lexicon old ON old.id=p.term_id JOIN lexicon current ON current.kind='near-v2' AND current.term=old.term "
        "WHERE old.kind>='near:' AND old.kind<'near;'"
    )
    connection.execute(
        "DELETE FROM postings WHERE term_id IN (SELECT id FROM lexicon WHERE kind>='near:' AND kind<'near;')"
    )
    connection.execute("DELETE FROM lexicon WHERE kind>='near:' AND kind<'near;'")
    connection.execute("INSERT INTO meta(key,value) VALUES('near_postings','global-v2') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")


_CONNECTIONS = ConnectionPool(_connect)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _features(fact):
    from commontrace import fact_conflicts
    from commontrace import hierarchical as h
    from commontrace.fact_index import _bm25_tokens

    words, numbers, negative = h._signature(fact.statement)
    content = h._content(words)
    from collections import Counter

    return (h._normalize_statement(fact.statement), _dump([numbers, sorted(negative)]),
            _dump(sorted(fact_conflicts.slot_key(fact.statement))), int(bool(negative)), _dump(numbers),
            content, Counter(_bm25_tokens(fact.statement)), h._fact_tokens(fact))


def _project(connection: sqlite3.Connection, fact) -> None:
    norm, block, slot, negative, numbers, content, frequencies, overlap = _features(fact)
    _remove_terms(connection, fact.id)
    previous = connection.execute("SELECT rowid FROM facts WHERE id=?", (fact.id,)).fetchone()
    if previous:
        connection.execute("DELETE FROM fact_fts WHERE rowid=?", previous)
    connection.execute(
        "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "payload=excluded.payload,norm=excluded.norm,block=excluded.block,slot=excluded.slot,"
        "negative=excluded.negative,numbers=excluded.numbers,status=excluded.status,"
        "forgotten=excluded.forgotten,category=excluded.category,confidence=excluded.confidence,length=excluded.length",
        (fact.id, _dump(fact.to_dict()), norm, block, slot, negative, numbers,
         fact.status, int(fact.forgotten), fact.category, fact.confidence, sum(frequencies.values())),
    )
    term_rows = [
        (kind, term, count) for kind, values in (
            ("near-v2", {word: 1 for word in content}),
            ("overlap-v1", {word: 1 for word in overlap}), ("bm25-v1", frequencies),
        ) for term, count in values.items()
    ]
    rowid = connection.execute("SELECT rowid FROM facts WHERE id=?", (fact.id,)).fetchone()[0]
    connection.executemany(
        "INSERT INTO lexicon(kind,term,documents) VALUES(?,?,1) "
        "ON CONFLICT(kind,term) DO UPDATE SET documents=documents+1",
        ((kind, term) for kind, term, _ in term_rows),
    )
    connection.executemany(
        "INSERT INTO postings SELECT id,?,? FROM lexicon WHERE kind=? AND term=?",
        ((rowid, count, kind, term) for kind, term, count in term_rows),
    )
    connection.execute("INSERT INTO fact_fts(rowid,fact_id,statement) VALUES(?,?,?)", (rowid, fact.id, fact.statement))


def _remove_terms(connection, identity):
    row = connection.execute("SELECT rowid FROM facts WHERE id=?", (identity,)).fetchone()
    if row:
        connection.execute("UPDATE lexicon SET documents=documents-1 WHERE id IN "
                           "(SELECT term_id FROM postings WHERE fact_row=?)", row)
        connection.execute("DELETE FROM postings WHERE fact_row=?", row)



def _remove_projection(connection, identity):
    _remove_terms(connection, identity)
    previous = connection.execute("SELECT rowid FROM facts WHERE id=?", (identity,)).fetchone()
    if previous:
        connection.execute("DELETE FROM fact_fts WHERE rowid=?", previous)
    connection.execute("DELETE FROM facts WHERE id=?", (identity,))


def _append(connection: sqlite3.Connection, fact, *, delete: bool = False,
            payload: dict[str, Any] | None = None) -> None:
    version = connection.execute("SELECT MAX(version) FROM events WHERE fact_id=?", (fact.id,)).fetchone()[0]
    version = (version or 0) + 1
    payload = fact.to_dict() if payload is None else payload
    relations = {"updates": [fact.id]} if version > 1 else {}
    record = MemoryRecord(
        fact.id, "fact", version, payload, status="active" if fact.status == "active" else "deprecated",
        relations=relations, provenance={"source_traces": fact.source_traces},
        scope={"labels": fact.scopes}, valid_from=fact.valid_from,
        valid_until=fact.valid_until, created_at=fact.created_at,
        retracted_at=fact.retracted_at, confidence=fact.confidence,
        evidence=payload["evidence"],
    )
    connection.execute("INSERT INTO events(fact_id,version,operation,record) VALUES(?,?,?,?)",
                       (fact.id, version, "remove" if delete else "put", _dump(vars(record))))
    if delete:
        _remove_projection(connection, fact.id)
    else:
        projected = connection.execute("SELECT payload FROM facts WHERE id=?", (fact.id,)).fetchone()
        if projected is None or projected[0] != _dump(payload):
            _project(connection, fact)


class FactMap(MutableMapping):
    """Lazy transaction-local facts. Only loaded/changed rows are inspected at commit."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.loaded: dict[str, Any] = {}
        self.before: dict[str, str | None] = {}
        self.removed: set[str] = set()

    def __getitem__(self, key):
        if key in self.removed:
            raise KeyError(key)
        if key not in self.loaded:
            row = self.connection.execute("SELECT payload FROM facts WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            from commontrace.hierarchical import _coerce_fact
            self.loaded[key] = _coerce_fact(json.loads(row[0]))
            self.before[key] = row[0]
        return self.loaded[key]

    def __setitem__(self, key, value):
        if key != value.id:
            raise ValueError("fact mapping key must equal its id")
        if key not in self.before:
            row = self.connection.execute("SELECT payload FROM facts WHERE id=?", (key,)).fetchone()
            self.before[key] = row[0] if row else None
        self.removed.discard(key)
        self.loaded[key] = value

    def __delitem__(self, key):
        self[key]
        self.removed.add(key)

    def __contains__(self, key):
        if key in self.removed:
            return False
        return key in self.loaded or self.connection.execute(
            "SELECT 1 FROM facts WHERE id=?", (key,)).fetchone() is not None

    def __iter__(self):
        seen = set()
        for (key,) in self.connection.execute("SELECT id FROM facts ORDER BY rowid"):
            if key not in self.removed:
                seen.add(key)
                yield key
        yield from (key for key in self.loaded if key not in seen and key not in self.removed)

    def __len__(self):
        total = self.connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        added = sum(
            key not in self.removed and before is None
            and self.connection.execute("SELECT 1 FROM facts WHERE id=?", (key,)).fetchone() is None
            for key, before in self.before.items()
        )
        removed = sum(self.connection.execute("SELECT 1 FROM facts WHERE id=?", (key,)).fetchone() is not None
                      for key in self.removed)
        return total + added - removed

    def flush(self):
        for key, fact in self.loaded.items():
            if key in self.removed:
                if self.before[key] is not None:
                    _append(self.connection, fact, delete=True)
                else:
                    _remove_projection(self.connection, key)
            else:
                payload = fact.to_dict()
                if _dump(payload) != self.before[key]:
                    _append(self.connection, fact, payload=payload)


@contextlib.contextmanager
def transaction(root: str, *, write: bool = False) -> Iterator[FactMap]:
    key, connection, generation = _borrow_connection(root)
    succeeded = False
    try:
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        facts = FactMap(connection)
        yield facts
        if write:
            facts.flush()
        connection.close_cursors()
        connection.commit()
        succeeded = True
    except BaseException:
        connection.rollback()
        raise
    finally:
        if succeeded:
            _return_connection(key, connection, generation)
        else:
            connection.close()


class _Norms:
    def __init__(self, index):
        self.index = index

    def get(self, norm, default=()):
        return self.index.candidates("norm", (norm,)) or default


class _Slots:
    def __init__(self, index):
        self.index = index

    def get(self, key, default=None):
        return _Variants(self.index, _dump(sorted(key[0])), int(bool(key[1])))


class _Variants:
    def __init__(self, index, slot, negative):
        self.index, self.slot, self.negative = index, slot, negative

    def __iter__(self):
        # Four distinct buckets suffice to veto the <=2-variant rule without
        # materializing large identifier-heavy slots. Staged adds are already
        # present in the transaction-local projection.
        rows = self.index.facts.connection.execute(
            "SELECT DISTINCT numbers FROM facts WHERE slot=? AND negative=? AND status='active' LIMIT 4",
            (self.slot, self.negative),
        )
        values = {tuple(json.loads(row[0])) for row in rows}
        return iter(sorted(values))

    def get(self, numbers, default=()):
        return self.index.candidates("slot",
                                     (self.slot, self.negative, _dump(numbers))) or default

    def __getitem__(self, numbers):
        return self.get(numbers)


class StatementIndex:
    """Persistent exact, lexical near-duplicate and contradiction candidate lookup."""

    def __init__(self, facts: FactMap):
        self.facts = facts
        self.by_norm, self.slots = _Norms(self), _Slots(self)

    def add(self, fact):
        _project(self.facts.connection, fact)

    def candidates(self, kind, arguments):
        queries = {
            "norm": "SELECT id FROM facts WHERE norm=? AND status='active' ORDER BY rowid",
            "slot": "SELECT id FROM facts WHERE slot=? AND negative=? AND numbers=? AND status='active'",
        }
        return [row[0] for row in self.facts.connection.execute(queries[kind], arguments)]

    def exact(self, statement, scopes):
        from commontrace import hierarchical as h
        norm = h._normalize_statement(statement)
        for key in self.by_norm.get(norm):
            fact = self.facts.get(key)
            if fact is not None and fact.status == "active" and h._normalize_statement(fact.statement) == norm \
                    and h._scopes_compatible(frozenset(scopes), fact.scopes):
                return fact
        return None

    def near(self, statement, scopes, threshold=0.85):
        from commontrace import hierarchical as h
        words, numbers, negative = h._signature(statement)
        content = h._content(words)
        if len(content) < 3:
            return None
        block = _dump([numbers, sorted(negative)])
        if self.facts.connection.execute(
            "SELECT 1 FROM facts INDEXED BY fact_block WHERE block=? AND status='active' LIMIT 1", (block,)
        ).fetchone() is None:
            return None
        need = int((1 - threshold) * len(content)) + 1
        counts = []
        for term in content:
            row = self.facts.connection.execute(
                "SELECT id,documents FROM lexicon WHERE kind='near-v2' AND term=?", (term,)).fetchone()
            counts.append((row[1] if row else 0, term, row[0] if row else None))
        candidates = set()
        # Any `need` distinct query terms preserve the Jaccard recall argument;
        # global document frequencies only choose their lookup order.
        for count, _term, identity in sorted(counts)[:need]:
            if identity is None:
                continue
            # Drive the intersection from its smaller input. Numeric-free
            # blocks can span the bank, whereas a common term can span many
            # tiny numbered blocks. This bounded count selects a query plan;
            # it never caps the returned candidates.
            block_count = self.facts.connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM facts INDEXED BY fact_block "
                "WHERE block=? AND status='active' LIMIT ?)", (block, count + 1)
            ).fetchone()[0]
            source = ("facts f INDEXED BY fact_block CROSS JOIN postings p" if block_count <= count
                      else "postings p CROSS JOIN facts f")
            candidates.update(row[0] for row in self.facts.connection.execute(
                "SELECT f.id FROM " + source + " "
                "WHERE f.block=? AND f.status='active' AND p.term_id=? AND p.fact_row=f.rowid", (block, identity)))
        best = None
        for key in sorted(candidates):
            fact = self.facts.get(key)
            if fact is not None and fact.status == "active" and not fact.forgotten \
                    and h._scopes_compatible(frozenset(scopes), fact.scopes) \
                    and h.near_duplicate(statement, fact.statement, threshold) \
                    and (best is None or fact.confirmations > best.confirmations):
                best = fact
        return best


def load(root: str):
    with transaction(root) as facts:
        return dict(facts)


def replace(root: str, values):
    with transaction(root, write=True) as facts:
        for key in list(facts):
            if key not in values:
                del facts[key]
        for key, value in values.items():
            facts[key] = value


def _checksum(rows) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update((_dump(row) + "\n").encode("utf-8"))
    return digest.hexdigest()


def migrate(root: str) -> dict[str, Any]:
    from commontrace.hierarchical import _coerce_fact, _facts_file

    destination = database_path(root)
    with _jsonl.locked(_facts_file(root)):
        if enabled(root):
            raise ValueError("store already migrated")
        source = _facts_file(root)
        raw = b""
        if os.path.exists(source):
            with open(source, "rb") as handle:
                raw = handle.read()
        values = {}
        for number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                fact = _coerce_fact(json.loads(line))
                if fact.id in values:
                    raise ValueError("duplicate fact id")
                values[fact.id] = fact
            except (TypeError, ValueError) as exc:
                raise ValueError(f"migration rejected row {number}: {exc}") from exc
        temporary = destination + ".migrating"
        # Retry is safe: an unpublished temporary store has no authoritative writes.
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(temporary + suffix):
                os.remove(temporary + suffix)
        connection = _connect(temporary, create=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for fact in values.values():
                _append(connection, fact)
            expected = _checksum(f.to_dict() for f in sorted(values.values(), key=lambda f: f.id))
            actual = _checksum(json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM facts ORDER BY id"))
            if expected != actual:
                raise ValueError("migration checksum mismatch")
            report = {"facts": len(values), "source_sha256": hashlib.sha256(raw).hexdigest(),
                      "normalized_sha256": expected, "destination_sha256": actual,
                      "source_preserved": True, "backend": "sqlite-wal", "schema_version": 2}
            connection.execute("INSERT INTO meta VALUES('migration',?)", (_dump(report),))
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        os.replace(temporary, destination)
        return report


def export(root: str, destination: str) -> None:
    if os.path.realpath(destination) in {
        os.path.realpath(database_path(root) + suffix) for suffix in ("", "-wal", "-shm")
    }:
        raise ValueError("export cannot overwrite the database or its journal")
    with transaction(root) as facts:
        _jsonl.write_rows(destination, (fact.to_dict() for fact in facts.values()))


def events(root: str) -> list[dict[str, Any]]:
    with transaction(root) as facts:
        result = []
        for seq, operation, record, latest in facts.connection.execute(
            "SELECT seq,operation,record,version=(SELECT MAX(e.version) FROM events e "
            "WHERE e.fact_id=events.fact_id) FROM events ORDER BY seq"
        ):
            envelope = json.loads(record)
            envelope["is_latest"] = bool(latest)
            result.append({"seq": seq, "operation": operation, "record": envelope})
        return result


def rebuild(root: str) -> None:
    from commontrace.hierarchical import _coerce_fact

    with transaction(root, write=True) as facts:
        connection = facts.connection
        connection.execute("DELETE FROM facts")
        connection.execute("DELETE FROM postings")
        connection.execute("UPDATE lexicon SET documents=0")
        connection.execute("DELETE FROM fact_fts")
        for identity, operation, record in connection.execute(
            "SELECT fact_id, operation, record FROM events WHERE seq IN (SELECT MAX(seq) FROM events GROUP BY fact_id)"
        ):
            if operation != "remove":
                _project(connection, _coerce_fact(json.loads(record)["payload"]))


def search(root: str, query: str, *, scope="", category="", as_of=None, limit=10,
           include_forgotten=False, show_expired=False, stability="", scorer="overlap-v1",
           known_at=None) -> list[tuple[AtomicFact, float]]:
    from commontrace import hierarchical as h
    from commontrace import lesson_cache, memory_authority, retrieval
    from commontrace.fact_evidence import EvidenceResolver
    from commontrace.fact_index import _bm25_tokens, _scorer, _terms

    selected = _scorer(scorer)
    if stability and stability not in h.STABILITY_TIERS:
        raise ValueError(f"unknown stability tier {stability!r}")
    moment = lesson_cache.parse_moment(as_of) if as_of else lesson_cache.parse_moment(h._now())
    known = lesson_cache.parse_moment(known_at) if known_at else None
    terms = _terms(query, selected)
    if limit <= 0 or (selected == "bm25-v1" and not terms):
        return []
    with transaction(root) as facts:
        def eligible(fact):
            return (not (fact.forgotten and not include_forgotten)
                    and (not scope or not fact.scopes or scope in fact.scopes)
                    and (not category or fact.category == category)
                    and (not stability or fact.stability == stability)
                    and ((h._valid_at(fact, moment) if as_of else fact.status == "active") if known is None
                         else h._known_at(fact, known) and (not as_of or h._valid_at(fact, moment)))
                    and (show_expired or not h._is_expired(fact, moment))
                    and (include_forgotten or not memory_authority.lineage_blocked(root, fact.id)))
        candidates = set()
        for term in terms:
            candidates.update(row[0] for row in facts.connection.execute(
                "SELECT fact_id FROM terms WHERE kind=? AND term=?", (selected, term)))
        if not terms:
            candidates = set(facts)
        count, average, frequencies = 0, 0.0, {}
        if selected == "bm25-v1":
            # Preserve the precise legacy scoped statistics contract. Optimizing
            # these filtered statistics is a remaining gate, not approximated here.
            from collections import Counter
            frequencies = Counter()
            for fact in facts.values():
                if eligible(fact):
                    tokens = _bm25_tokens(fact.statement)
                    count += 1
                    average += len(tokens)
                    frequencies.update(set(tokens))
            average = average / count if count else 0.0
        scores = []
        for key in candidates:
            fact = facts[key]
            if not eligible(fact):
                continue
            if not terms:
                score = fact.confidence
            elif selected == "overlap-v1":
                tokens = h._fact_tokens(fact)
                overlap = len(terms & tokens)
                score = round(0.7 * overlap / len(terms | tokens) + fact.confidence * 0.3, 4)
            else:
                from collections import Counter
                tokens = _bm25_tokens(fact.statement)
                counts = Counter(tokens)
                raw = sum(retrieval._bm25_term(counts[term], retrieval._idf(count, frequencies[term]),
                                              len(tokens), average)
                          for term in sorted(terms) if counts[term] and frequencies[term])
                score = round(0.7 * raw / (1 + raw) + fact.confidence * 0.3, 4)
            scores.append((fact, score))
        scores.sort(key=lambda pair: (-pair[1], pair[0].id))
        resolver = EvidenceResolver(root, facts, as_of=as_of)
        result = []
        for fact, score in scores:
            if fact.evidence_bound and not resolver.assess(fact.id).eligible:
                continue
            result.append((fact, score))
            if len(result) >= limit:
                break
        return result


class ReadView(Mapping):
    """Detached generation-bound view, owning no open connection between reads."""

    def __init__(self, root):
        self.root = root
        with transaction(root) as facts:
            self.generation = facts.connection.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]

    def ensure_current(self):
        with transaction(self.root) as facts:
            self._check(facts)

    def _check(self, facts):
        if facts.connection.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0] != self.generation:
            from commontrace.fact_index import FactSnapshotChanged
            raise FactSnapshotChanged("SQLite fact generation changed")

    def __getitem__(self, key):
        with transaction(self.root) as facts:
            self._check(facts)
            return facts[key]

    def __iter__(self):
        with transaction(self.root) as facts:
            self._check(facts)
            return iter(list(facts))

    def __len__(self):
        with transaction(self.root) as facts:
            self._check(facts)
            return len(facts)


def read_view(root: str) -> ReadView:
    return ReadView(root)


def fts_candidates(root: str, query: str, *, context: list[str] | None = None, limit=200, as_of=None):
    """Bounded FTS5 BM25 candidates, fresh temporal/proof/scope admission.

    This channel uses SQLite's index statistics; the historical ``bm25-v1`` API
    retains its precise scoped-statistics contract. A new candidate channel does
    not silently redefine legacy scores.
    """
    from commontrace import hierarchical as h
    from commontrace import lesson_cache, memory_authority, memory_control
    from commontrace.fact_evidence import EvidenceResolver
    from commontrace.search_recipes import terms

    tokens = list(dict.fromkeys(terms(query)))[:32]
    if not tokens or limit <= 0:
        return []
    expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    moment = lesson_cache.parse_moment(as_of or h._now())
    with transaction(root) as facts:
        rows = facts.connection.execute(
            "SELECT f.id,bm25(fact_fts) FROM fact_fts "
            "JOIN facts f ON f.rowid=fact_fts.rowid WHERE fact_fts MATCH ? "
            "AND f.forgotten=0 ORDER BY bm25(fact_fts) LIMIT ?",
            (expression, max(1000, min(10000, limit * 10))),
        )
        resolver = EvidenceResolver(root, facts, as_of=as_of)
        result = []
        for identity, score in rows:
            fact = facts[identity]
            if (
                fact.status != "active"
                and not as_of
                or not h._valid_at(fact, moment)
                or h._is_expired(fact, moment)
                or not memory_control.matches(fact.scopes, context or [])
                or not memory_authority.permits(root, fact)
                or fact.evidence_bound
                and not resolver.assess(identity).eligible
            ):
                continue
            result.append((fact, float(-score)))
            if len(result) >= limit:
                break
        return result
