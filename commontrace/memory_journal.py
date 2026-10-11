"""Versioned memory and capture journal. SQLite projections are disposable.

This is the envelope store for new agent-runtime records; existing governed fact
and lesson APIs retain their wire formats and are imported through ``put``.
Private captures, cursors and injection claims share the same WAL transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace

from commontrace import memory_guard, paths
from commontrace.memory_record import MemoryRecord, facet_observation
from commontrace.record_visibility import bind_capture_sources, now, sanitize_record_content
from commontrace.record_visibility import moment as moment
from commontrace.record_visibility import visible as _visible
from commontrace.runtime_cache import RuntimeCache
from commontrace.sqlite_pool import Connection, ConnectionPool


def _connect(path: str) -> Connection:
    return sqlite3.connect(path, timeout=2, isolation_level=None, check_same_thread=False, factory=Connection)


_CONNECTIONS = ConnectionPool(_connect)


def close_connections(root: str | None = None) -> None:
    """Drain idle journal handles before removing or replacing a quiescent store."""
    _CONNECTIONS.close(os.path.join(paths.memory_dir(root), "runtime.sqlite3") if root is not None else None)

_INITIALIZED = RuntimeCache(max_entries=128, max_bytes=256 * 1024, ttl=600, weigh=lambda key, value: len(repr(key)) + 1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS record_events(id TEXT, version INTEGER, body TEXT, PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, kind TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS record_facets ON records(kind, json_extract(body,'$.payload.entity'),
 json_extract(body,'$.payload.facet'), json_extract(body,'$.scope'));
CREATE INDEX IF NOT EXISTS record_status_kind ON records(json_extract(body,'$.status'),kind,id);
CREATE INDEX IF NOT EXISTS record_always ON records(kind,json_extract(body,'$.payload.critical'),
 json_extract(body,'$.payload.lesson_kind'));
CREATE VIRTUAL TABLE IF NOT EXISTS record_fts USING fts5(id UNINDEXED,text);
CREATE TABLE IF NOT EXISTS links(source TEXT,derived TEXT,PRIMARY KEY(source,derived));
CREATE TABLE IF NOT EXISTS captures(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,session TEXT,event TEXT,
 body TEXT,scope TEXT);
CREATE INDEX IF NOT EXISTS capture_session ON captures(session,sequence);
CREATE TABLE IF NOT EXISTS cursors(session TEXT PRIMARY KEY,through INTEGER);
CREATE TABLE IF NOT EXISTS injected(session TEXT,id TEXT,PRIMARY KEY(session,id));
CREATE TABLE IF NOT EXISTS decisions(session TEXT,id TEXT,PRIMARY KEY(session,id));
CREATE TABLE IF NOT EXISTS studies(id TEXT PRIMARY KEY,scope TEXT,actor TEXT,remaining INTEGER,
expires TEXT,revision TEXT);
CREATE TABLE IF NOT EXISTS vectors(id TEXT,model TEXT,digest TEXT,vector TEXT,PRIMARY KEY(id,model,digest));
CREATE TABLE IF NOT EXISTS erasures(sequence INTEGER PRIMARY KEY,body TEXT);
CREATE TABLE IF NOT EXISTS deliveries(token TEXT PRIMARY KEY,session TEXT,scope TEXT,body TEXT,committed INTEGER);
"""


def visible(record: MemoryRecord, scope: dict, *, statuses=("active",), at=None) -> bool:
    from commontrace.memory_control import REQUIRED_PRINCIPAL_SCOPE

    required = REQUIRED_PRINCIPAL_SCOPE.get()
    if required and record.scope.get("agent") != required.partition(":")[2]:
        return False
    return _visible(record, scope, statuses=statuses, at=at)


class Journal:
    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(paths.memory_dir(root), "runtime.sqlite3")
        paths.enforce_boundary(root, self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)

        def initialize():
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(SCHEMA)
            return True

        stamp = os.stat(self.path)
        _INITIALIZED.get_or_load((self.path, stamp.st_dev, stamp.st_ino), initialize)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, *, write=False):
        key, db, generation = _CONNECTIONS.borrow(self.path)
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.close_cursors()
            if write:
                db.commit()
            elif db.in_transaction:
                db.rollback()
        except BaseException:
            db.close()
            raise
        else:
            _CONNECTIONS.release(key, db, generation)

    def put(self, record: MemoryRecord, *, expected_version: int | None = None) -> MemoryRecord:
        with self.connect(write=True) as db:
            return self._put(db, record, expected_version=expected_version)

    def put_many(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        """One lifecycle operation, including target deprecation, commits atomically."""
        if len(records) > 32:
            raise ValueError("record transaction exceeds 32 versions")
        with self.connect(write=True) as db:
            return [self._put(db, record) for record in records]

    def _put(self, db, record: MemoryRecord, *, expected_version=None) -> MemoryRecord:
        record = bind_capture_sources(sanitize_record_content(record))
        prior = db.execute("SELECT body FROM records WHERE id=?", (record.id,)).fetchone()
        old = MemoryRecord(**json.loads(prior[0])) if prior else None
        if (
            expected_version is not None
            and expected_version != (old.version if old else 0)
            or record.version != (old.version + 1 if old else 1)
        ):
            raise ValueError("record version conflict")
        if old and (old.kind != record.kind or old.scope != record.scope):
            raise ValueError("record identity cannot change kind or scope")
        for ids in record.relations.values():
            for source in ids:
                source_row = db.execute("SELECT body FROM records WHERE id=?", (source,)).fetchone()
                if source_row and json.loads(source_row[0])["scope"] != record.scope:
                    raise ValueError("relationships cannot cross scopes")
                capture = db.execute("SELECT scope FROM captures WHERE id=?", (source,)).fetchone()
                if capture and json.loads(capture[0]) != record.scope:
                    raise ValueError("capture evidence cannot cross scopes")
                db.execute("INSERT OR IGNORE INTO links VALUES(?,?)", (source, record.id))
        # Forward references cannot acquire a different scope later.
        for row in db.execute(
            "SELECT r.body FROM links l JOIN records r ON r.id=l.derived WHERE l.source=?", (record.id,)
        ):
            if json.loads(row[0])["scope"] != record.scope:
                raise ValueError("incoming relationships cannot cross scopes")
        body = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
        db.execute("INSERT INTO record_events VALUES(?,?,?)", (record.id, record.version, body))
        db.execute(
            "INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (record.id, record.kind, body),
        )
        db.execute("DELETE FROM record_fts WHERE id=?", (record.id,))
        db.execute("INSERT INTO record_fts VALUES(?,?)", (record.id, json.dumps(record.payload, ensure_ascii=False)))
        if record.kind == "fact":
            self._refresh_facet(db, record)
        return record

    def _refresh_facet(self, db, record):
        entity, facet = record.payload.get("entity"), record.payload.get("facet")
        if not isinstance(entity, str) or not isinstance(facet, str) or not entity.strip() or not facet.strip():
            return
        rows = db.execute(
            "SELECT body FROM records WHERE kind='fact' AND json_extract(body,'$.payload.entity')=? "
            "AND json_extract(body,'$.payload.facet')=? AND json_extract(body,'$.scope')=? ORDER BY rowid DESC",
            (entity, facet, json.dumps(record.scope, sort_keys=True, separators=(",", ":"))),
        )
        sources = []
        for row in rows:
            source = MemoryRecord(**json.loads(row[0]))
            if source.id != record.id and visible(source, record.scope):
                sources.append(source)
                if len(sources) == 8:
                    break
        if visible(record, record.scope):
            sources.append(record)
        if not sources:
            return  # existing revision-bound observation is withheld by grounding
        observation = facet_observation(entity, facet, sources)
        prior = db.execute("SELECT body FROM records WHERE id=?", (observation.id,)).fetchone()
        if prior:
            observation = facet_observation(entity, facet, sources, previous=MemoryRecord(**json.loads(prior[0])))
        self._put(db, observation)

    def history(self, identity: str) -> list[MemoryRecord]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM record_events WHERE id=? ORDER BY version", (identity,)).fetchall()
        return [replace(MemoryRecord(**json.loads(row[0])), is_latest=i == len(rows) - 1) for i, row in enumerate(rows)]

    def latest(self, identity: str) -> MemoryRecord | None:
        """Indexed revision pointer for writers; readers use grounded current/search."""
        with self.connect() as db:
            row = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
        return MemoryRecord(**json.loads(row[0])) if row else None

    def current(self, identity: str, *, scope: dict, statuses=("active",), at=None) -> MemoryRecord | None:
        """Indexed canonical lookup with the same evidence checks as search."""
        with self.connect() as db:
            row = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
            if row is None:
                return None
            record = MemoryRecord(**json.loads(row[0]))
            if not visible(record, scope, statuses=statuses, at=at):
                return None
            return record if self._grounded(db, record, scope, statuses=statuses, at=at) else None

    def _grounded(self, db, record, scope, *, statuses, at, seen=None):
        if record.provenance.get("code_evidence"):
            from commontrace.record_visibility import code_evidence_current

            if not code_evidence_current(self.root, record.provenance["code_evidence"]):
                return False
        seen = set() if seen is None else seen
        if record.id in seen or len(seen) >= 64:
            return False  # cycles and unbounded grounding fail closed
        seen.add(record.id)
        for source in record.relations.get("derives", []):
            row = db.execute("SELECT body FROM records WHERE id=?", (source,)).fetchone()
            if row is None:
                return False
            parent = MemoryRecord(**json.loads(row[0]))
            versions = record.provenance.get("source_versions")
            if versions is not None and (not isinstance(versions, dict) or versions.get(source) != parent.version):
                return False
            if not visible(parent, scope, statuses=("active",), at=at) or not self._grounded(
                db, parent, scope, statuses=statuses, at=at, seen=set(seen)
            ):
                return False
        return True

    def search(
        self, query: str, *, scope: dict | None = None, kinds=(), statuses=("active",), limit=20, at=None, always=False
    ) -> list[MemoryRecord]:
        from commontrace.search_recipes import terms

        if limit <= 0:
            return []
        tokens = list(dict.fromkeys(terms(query)))[:32]
        with self.connect() as db:
            clauses = ["json_extract(r.body,'$.is_latest')=1"]
            parameters = []
            if statuses:
                clauses.append("json_extract(r.body,'$.status') IN (SELECT value FROM json_each(?))")
                parameters.append(json.dumps(list(statuses)))
            else:
                return []
            if kinds:
                clauses.append("r.kind IN (SELECT value FROM json_each(?))")
                parameters.append(json.dumps(list(kinds)))
            for dimension in ("tenant", "user", "agent", "session", "repo"):
                field = "json_extract(r.body,'$.scope." + dimension + "')"
                clauses.append("(" + field + " IS NULL OR " + field + "=?)")
                parameters.append((scope or {}).get(dimension))
            if always:
                clauses.append(
                    "(r.kind='directive' OR (r.kind='profile' AND json_extract(r.body,'$.payload.critical')=1) "
                               "OR (r.kind='lesson' AND json_extract(r.body,'$.payload.lesson_kind')='directive'))")
            source = "records r"
            order = "r.id"
            if tokens:
                expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
                source = "record_fts f JOIN records r ON r.id=f.id"
                clauses.append("record_fts MATCH ?")
                parameters.append(expression)
                order = "bm25(record_fts)"
            # Identifiers and predicates above are fixed internal literals; every caller value is bound.
            rows = db.execute("SELECT r.body FROM " + source + " WHERE " + " AND ".join(clauses) +  # nosec B608
                              " ORDER BY " + order, parameters)
            result = []
            for row in rows:
                record = MemoryRecord(**json.loads(row[0]))
                if (not kinds or record.kind in kinds) and visible(record, scope or {}, statuses=statuses, at=at):
                    if not self._grounded(db, record, scope or {}, statuses=statuses, at=at):
                        continue
                    result.append(record)
                    if len(result) >= limit:
                        break
            return result

    def capture(self, session: str, event: str, payload: dict, *, scope: dict, event_id: str | None = None) -> dict:
        if not session or len(session) > 256 or len(json.dumps(payload)) > 100000:
            raise ValueError("capture exceeds bounded session or payload")
        if event_id is not None and (not isinstance(event_id, str) or not event_id or len(event_id) > 256):
            raise ValueError("capture id must be a bounded nonempty string")
        payload, _ = memory_guard.sanitize_metadata(payload, pii=True)
        identity = event_id or uuid.uuid4().hex
        with self.connect(write=True) as db:
            for row in db.execute(
                "SELECT r.body FROM links l JOIN records r ON r.id=l.derived WHERE l.source=?", (identity,)
            ):
                if json.loads(row[0])["scope"] != scope:
                    raise ValueError("forward capture evidence cannot cross scopes")
            db.execute(
                "INSERT OR IGNORE INTO captures(id,session,event,body,scope) VALUES(?,?,?,?,?)",
                (identity, session, event, json.dumps(payload), json.dumps(scope, sort_keys=True)),
            )
            row = db.execute(
                "SELECT sequence,session,event,body,scope FROM captures WHERE id=?", (identity,)
            ).fetchone()
            if row[1] != session or json.loads(row[4]) != scope or row[2] != event or json.loads(row[3]) != payload:
                raise ValueError("capture identity belongs to another session")
            return {"id": identity, "sequence": row[0], "event": row[2], "payload": json.loads(row[3]), "scope": scope}

    def buffer(self, session: str, *, scope: dict, limit=128) -> list[dict]:
        with self.connect() as db:
            return [
                {"sequence": row[0], "id": row[1], "event": row[2], "payload": json.loads(row[3])}
                for row in db.execute(
                    "SELECT sequence,id,event,body FROM captures WHERE session=? AND scope=? "
                    "AND sequence>COALESCE((SELECT through FROM cursors WHERE session=?),0) "
                    "ORDER BY sequence LIMIT ?",
                    (session, json.dumps(scope, sort_keys=True), session, limit),
                )
            ]

    def seal(self, session: str, through: int):
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO cursors VALUES(?,?) ON CONFLICT(session) DO UPDATE SET through="
                "MAX(through,excluded.through)",
                (session, through),
            )

    def closed_boundary(self, session: str, *, scope: dict) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT sequence,id,event,body FROM captures WHERE session=? AND scope=? "
                "AND event IN ('Stop','SessionEnd','SubagentStop') "
                "AND sequence>COALESCE((SELECT through FROM cursors WHERE session=?),0) "
                "ORDER BY sequence LIMIT 1",
                (session, json.dumps(scope, sort_keys=True), session),
            ).fetchone()
            return {"sequence": row[0], "id": row[1], "event": row[2], "payload": json.loads(row[3])} if row else None

    def claim_injections(self, session: str, identities: list[str]) -> list[str]:
        with self.connect(write=True) as db:
            fresh = []
            for identity in identities:
                if db.execute("INSERT OR IGNORE INTO injected VALUES(?,?)", (session, identity)).rowcount:
                    fresh.append(identity)
            return fresh

    def inherit(self, session: str, parent: str):
        with self.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO injected SELECT ?,id FROM injected WHERE session=?", (session, parent))
            db.execute("INSERT OR IGNORE INTO decisions SELECT ?,id FROM decisions WHERE session=?", (session, parent))

    def unclaimed(self, session: str, identities: list[str]) -> list[str]:
        with self.connect() as db:
            seen = {
                row[0]
                for row in db.execute(
                    "SELECT id FROM injected WHERE session=? UNION SELECT id FROM decisions WHERE session=?",
                    (session, session),
                )
            }
        return [identity for identity in identities if identity not in seen]

    def offer_delivery(self, session: str, identities: list[str], assignments: list[dict], *, scope: dict) -> str:
        token = uuid.uuid4().hex
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO deliveries VALUES(?,?,?,?,0)",
                (
                    token,
                    session,
                    json.dumps(scope, sort_keys=True),
                    json.dumps({"ids": identities, "assignments": assignments}),
                ),
            )
        return token

    def commit_delivery(self, token: str, *, scope: dict) -> list[str]:
        # Write-ahead receipt survives a broker crash. Replayed identical assignment rows
        # are safely normalized by the existing causal analyzer.
        from commontrace import frontmatter, holdout_io

        path = os.path.join(os.path.dirname(self.path), "holdout_log.jsonl")
        with frontmatter.locked(path), self.connect(write=True) as db:
            row = db.execute("SELECT session,scope,body,committed FROM deliveries WHERE token=?", (token,)).fetchone()
            if row is None or json.loads(row[1]) != scope:
                raise ValueError("delivery receipt does not belong to this scope")
            body = json.loads(row[2])
            if row[3]:
                return body["ids"]
            for identity in body["ids"]:
                db.execute("INSERT OR IGNORE INTO injected VALUES(?,?)", (row[0], identity))
            for assignment in body["assignments"]:
                identity = assignment["lesson"]
                inserted = db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?)", (row[0], identity)).rowcount
                if inserted:
                    db.execute("UPDATE studies SET remaining=MAX(0,remaining-1) WHERE id=?", (identity,))
            if body["assignments"]:
                holdout_io._append_lines(path, [json.dumps(item) for item in body["assignments"]])
            db.execute("UPDATE deliveries SET committed=1 WHERE token=?", (token,))
            return body["ids"]

    def authorize_evaluation(self, identity: str, *, actor: str, occasions: int, expires: str):
        from commontrace.measure import content_revision

        history = self.history(identity)
        if not history or history[-1].kind != "lesson" or history[-1].status != "shadow":
            raise ValueError("controlled evaluations require a reviewed shadow lesson")
        if not actor or type(occasions) is not int or not 1 <= occasions <= 10000 or moment(expires) <= moment(now()):
            raise ValueError("evaluation requires an actor, 1..10000 occasions and a future expiration")
        record = history[-1]
        with self.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO studies VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    json.dumps(record.scope, sort_keys=True),
                    actor,
                    occasions,
                    expires,
                    content_revision(record.payload.get("text", "")),
                ),
            )
        return {"id": identity, "actor": actor, "occasions": occasions, "expires": expires, "controlled": True}

    def evaluations(self, scope: dict) -> dict[str, str]:
        with self.connect() as db:
            return {
                identity: revision
                for identity, revision, expires in db.execute(
                    "SELECT id,revision,expires FROM studies WHERE scope=? AND remaining>0",
                    (json.dumps(scope, sort_keys=True),),
                )
                if moment(expires) > moment(now())
            }

    def cache_vector(self, identity, model, text, vector):
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", (identity, model, digest, json.dumps(vector)))

    def vectors(self, records: list[tuple[MemoryRecord, str]], model: str) -> list:
        with self.connect() as db:
            result = []
            for record, text in records:
                row = db.execute(
                    "SELECT vector FROM vectors WHERE id=? AND model=? AND digest=?",
                    (record.id, model, hashlib.sha256(text.encode()).hexdigest()),
                ).fetchone()
                result.append(json.loads(row[0]) if row else None)
            return result

    def vector(self, identity, model, text):
        with self.connect() as db:
            row = db.execute(
                "SELECT vector FROM vectors WHERE id=? AND model=? AND digest=?",
                (identity, model, hashlib.sha256(text.encode()).hexdigest()),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def reap_due(self, *, limit=16) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT body FROM records WHERE julianday(json_extract(body,'$.payload.forget_after'))"
                "<=julianday(?) LIMIT ?",
                (now(), limit),
            ).fetchall()
        result = []
        for (body,) in rows:
            record = MemoryRecord(**json.loads(body))
            try:
                result.append(
                    self.forget(record.id, reason=record.payload["forget_reason"], expected_version=record.version)
                )
            except ValueError:
                continue
        return result

    def forget(self, identity: str, *, reason: str, expected_version: int | None = None) -> dict:
        if not reason.strip():
            raise ValueError("erasure requires a reason")
        with self.connect(write=True) as db:
            db.execute("PRAGMA secure_delete=ON")
            if expected_version is not None:
                current = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
                if current is None or json.loads(current[0])["version"] != expected_version:
                    raise ValueError("expiration record version changed")
            pending, deleted, capture_ids = [identity], set(), set()
            while pending:
                current = pending.pop()
                if current in deleted:
                    continue
                deleted.add(current)
                pending.extend(row[0] for row in db.execute("SELECT derived FROM links WHERE source=?", (current,)))
                # Every historical revision contributes source captures, not only the latest payload.
                for (body,) in db.execute("SELECT body FROM record_events WHERE id=?", (current,)):
                    record = MemoryRecord(**json.loads(body))
                    capture_ids.add((current, json.dumps(record.scope, sort_keys=True)))
                    for source in set(record.payload.get("source_message_ids", [])) | set(
                        record.relations.get("caused_by", [])
                    ):
                        row = db.execute("SELECT scope FROM captures WHERE id=?", (source,)).fetchone()
                        if row and json.loads(row[0]) == record.scope:
                            capture_ids.add((source, row[0]))
                            # Other records distilled from an erased capture lose their source too.
                            pending.extend(
                                value[0]
                                for value in db.execute(
                                    "SELECT l.derived FROM links l JOIN records r ON r.id=l.derived "
                                    "WHERE l.source=? AND json_extract(r.body,'$.scope')=json(?)",
                                    (source, json.dumps(record.scope, sort_keys=True)),
                                )
                            )
            for capture_id, scope in capture_ids:
                db.execute("DELETE FROM captures WHERE id=? AND scope=?", (capture_id, scope))
            for token, body in db.execute("SELECT token,body FROM deliveries").fetchall():
                receipt = json.loads(body)
                assigned = {row.get("lesson") for row in receipt["assignments"]}
                if (set(receipt["ids"]) | assigned) & deleted:
                    db.execute("DELETE FROM deliveries WHERE token=?", (token,))
            for current in deleted:
                for table in ("record_events", "records", "record_fts", "vectors", "injected", "decisions", "studies"):
                    db.execute("DELETE FROM " + table + " WHERE id=?", (current,))  # nosec B608 - fixed table list
                db.execute("DELETE FROM links WHERE source=? OR derived=?", (current, current))
            db.execute("INSERT INTO record_fts(record_fts) VALUES('rebuild')")
            audit = {
                "at": now(),
                "deleted": sorted(deleted),
                "reason": memory_guard.sanitize_metadata({"reason": reason}, pii=True)[0]["reason"],
            }
            db.execute("INSERT INTO erasures(body) VALUES(?)", (json.dumps(audit),))
        # Purge WAL copies before reporting completion. Backups remain the owner's responsibility.
        close_connections(self.root)
        with self.connect() as db:
            db.execute("VACUUM")
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("erasure checkpoint blocked; close readers and retry")
        from commontrace import kg_similarity

        kg_similarity._QUERY_VECTORS.clear()
        return audit
