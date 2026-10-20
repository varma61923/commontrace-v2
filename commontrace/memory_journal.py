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
from typing import Any

from commontrace import capabilities, memory_guard, paths
from commontrace.memory_record import MemoryRecord, facet_observation
from commontrace.record_visibility import GroundingState, bind_capture_sources, now, sanitize_record_content
from commontrace.record_visibility import moment as moment
from commontrace.record_visibility import visible as _visible
from commontrace.runtime_cache import RuntimeCache
from commontrace.sqlite_pool import Connection, ConnectionPool


def _connect(path: str) -> Connection:
    return sqlite3.connect(path, timeout=2, isolation_level=None, check_same_thread=False, factory=Connection)


_CONNECTIONS = ConnectionPool(_connect)


def close_connections(root: str | None = None) -> None:
    """Drain idle handles and init metadata before removing a quiescent store."""
    if root is None:
        _CONNECTIONS.close()
        return
    path = os.path.join(paths.memory_dir(root), "runtime.sqlite3")
    _CONNECTIONS.close(path)
    try:
        stamp = os.stat(path)
    except OSError:
        return
    _INITIALIZED.invalidate((path, stamp.st_dev, stamp.st_ino))

_INITIALIZED = RuntimeCache(max_entries=128, max_bytes=256 * 1024, ttl=600, weigh=lambda key, value: len(repr(key)) + 1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS record_events(id TEXT, version INTEGER, body TEXT, PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, kind TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS resource_refs(ref TEXT PRIMARY KEY,id TEXT NOT NULL,kind TEXT NOT NULL,UNIQUE(kind,id));
CREATE TABLE IF NOT EXISTS authority_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS change_outbox(event_id TEXT PRIMARY KEY,body TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS scoped_cursors(session TEXT,scope TEXT,through INTEGER,
 PRIMARY KEY(session,scope));
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
        self._draining_authority = False
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
        if write and not self._draining_authority:
            try:
                self._drain_authority()
            except (OSError, ValueError, PermissionError, RuntimeError, sqlite3.Error) as exc:
                import logging

                logging.getLogger(__name__).warning("UniMem committed runtime authority metadata awaits recovery: %s",
                                                    type(exc).__name__)

    def _delegated(self):
        return capabilities.CURRENT.get() is not None or os.path.isfile(
            os.path.join(paths.memory_dir(self.root), "capabilities.json"))

    def _runtime_readable(self, record):
        from commontrace import capability_io, policy_io, policy_relations

        if not policy_io.permits(self.root, capability_io.runtime_descriptor(record)):
            return False
        if record.id not in policy_relations.authorized_ids(self.root, [record.id], "records"):
            return False
        if not self._delegated():
            return True

        return capability_io.runtime_permits(self.root, record)

    def _queue_authority(self, db, signed):
        if signed is not None:
            if db.execute("SELECT count(*) FROM authority_outbox").fetchone()[0] >= 10000:
                raise RuntimeError("UniMem runtime authority outbox requires recovery")
            db.execute("INSERT INTO authority_outbox(body) VALUES(?)", (json.dumps(signed, sort_keys=True),))

    def _drain_authority(self):
        if self._draining_authority or not self._delegated():
            return
        from commontrace import _jsonl, lineage

        self._draining_authority = True
        try:
            with _jsonl.locked(self.path + ".authority"):
                with self.connect() as db:
                    rows = db.execute("SELECT sequence,body FROM authority_outbox "
                                      "ORDER BY sequence LIMIT 10000").fetchall()
                for sequence, body in rows:
                    lineage.commit_prepared(self.root, json.loads(body))
                    with self.connect(write=True) as db:
                        db.execute("DELETE FROM authority_outbox WHERE sequence=?", (sequence,))
        finally:
            self._draining_authority = False

    def _stage_change(self, db, resource_kind, identity, scope, revision, kind):
        from commontrace import change_stream

        streaming = change_stream.enabled(self.root)
        if not streaming and not self._delegated():
            return
        from commontrace import capability_io
        from commontrace import frontier_protocol as fp

        reference = capability_io.runtime_identity(self.root, identity, capture=resource_kind == "capture")
        if kind != "erase":
            db.execute("INSERT OR REPLACE INTO resource_refs VALUES(?,?,?)", (reference, identity, resource_kind))
        if streaming:
            if db.execute("SELECT count(*) FROM change_outbox").fetchone()[0] >= 10000:
                raise RuntimeError("UniMem runtime change outbox requires recovery")
            body = {"kind": kind, "target": reference, "scopes": capability_io.scope_labels(scope),
                    "namespace": "records", "revision": revision}
            db.execute("INSERT INTO change_outbox VALUES(?,?)", (uuid.uuid4().hex, fp.wire(body).decode()))

    def drain_changes(self) -> None:
        """Drain only committed metadata, with no SQLite write lock held across delivery locks."""
        from commontrace import change_stream

        self._drain_authority()
        with self.connect() as db:
            rows = db.execute("SELECT event_id,body FROM change_outbox ORDER BY rowid LIMIT 10000").fetchall()
        for event_id, raw in rows:
            body = json.loads(raw)
            change_stream.publish(self.root, body.pop("kind"), body.pop("target"), event_id=event_id, **body)
            with self.connect(write=True) as db:
                db.execute("DELETE FROM change_outbox WHERE event_id=?", (event_id,))

    def admit_reference(self, reference: str, *, scope: dict[str, Any]) -> dict[str, Any] | None:
        from commontrace import capability_io, memory_authority
        from commontrace import frontier_protocol as fp

        self._drain_authority()
        if memory_authority.lineage_blocked(self.root, reference):
            return None
        with self.connect() as db:
            row = db.execute("SELECT id,kind FROM resource_refs WHERE ref=?", (reference,)).fetchone()
            if row is None:
                return None
            if row[1] == "record":
                record = self.current(row[0], scope=scope)
                if record is None:
                    return None
                return {"id": reference, "text": json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
                        "revision": str(record.version), "content_digest": fp.digest(record.to_dict()),
                        "scopes": capability_io.scope_labels(record.scope),
                        "namespace": "records", "memory_class": record.kind}
            capture = db.execute("SELECT session,event,body,scope FROM captures WHERE id=?", (row[0],)).fetchone()
            if capture is None:
                return None
            actual = json.loads(capture[3])
            if (not all(scope.get(key) == value for key, value in actual.items() if key != "labels")
                    or not set(actual.get("labels", [])) <= set(scope.get("labels", []))):
                return None
            body = capability_io.capture_descriptor(row[0], capture[0], capture[1], json.loads(capture[2]), actual)
            from commontrace import policy_io

            if not policy_io.permits(self.root, body):
                return None
            from commontrace import policy_relations

            if row[0] not in policy_relations.authorized_ids(self.root, [row[0]], "records"):
                return None
            if not capability_io.permits(self.root, body, namespace="records"):
                return None
            return {"id": reference, "text": json.dumps(body["payload"], ensure_ascii=False, sort_keys=True),
                    "revision": fp.digest(body), "content_digest": fp.digest(body), "scopes": body["scopes"],
                    "namespace": "records", "memory_class": "capture"}

    def _delegated_search(self, query, *, scope, kinds, statuses, limit, at, always):
        from commontrace import capability_io
        from commontrace import frontier_protocol as fp
        from commontrace.search_recipes import _bm25, terms

        fp.count(limit, 1, 1000)
        if not statuses:
            return []
        clauses = ["json_extract(body,'$.is_latest')=1",
                   "json_extract(body,'$.status') IN (SELECT value FROM json_each(?))"]
        parameters = [json.dumps(list(statuses))]
        if kinds:
            clauses.append("kind IN (SELECT value FROM json_each(?))")
            parameters.append(json.dumps(list(kinds)))
        for dimension in ("tenant", "user", "agent", "session", "repo"):
            field = "json_extract(body,'$.scope." + dimension + "')"
            clauses.append("(" + field + " IS NULL OR " + field + "=?)")
            parameters.append(scope.get(dimension))
        clauses.append("NOT EXISTS (SELECT 1 FROM json_each(json_extract(body,'$.scope.labels')) "
                       "WHERE value NOT IN (SELECT value FROM json_each(?)))")
        parameters.append(json.dumps(scope.get("labels", [])))
        value = capability_io.current(self.root)
        if value is not None:
            from commontrace.record_visibility import scope_from_labels

            rules = value["verified"]["caveats"]
            if "records" not in rules["namespaces"] or "read" not in rules["operations"]:
                return []
            clauses.append("kind IN (SELECT value FROM json_each(?))")
            parameters.append(json.dumps(rules["classes"]))
            ceiling = scope_from_labels(rules["labels"])
            for dimension in ("tenant", "user", "agent", "session", "repo"):
                field = "json_extract(body,'$.scope." + dimension + "')"
                clauses.append("(" + field + " IS NULL OR " + field + "=?)")
                parameters.append(ceiling.get(dimension))
            clauses.append("NOT EXISTS (SELECT 1 FROM json_each(json_extract(body,'$.scope.labels')) "
                           "WHERE value NOT IN (SELECT value FROM json_each(?)))")
            parameters.append(json.dumps(ceiling.get("labels", [])))
        if always:
            clauses.append("(kind='directive' OR (kind='profile' AND json_extract(body,'$.payload.critical')=1) "
                           "OR (kind='lesson' AND json_extract(body,'$.payload.lesson_kind')='directive'))")
        with self.connect() as db:
            db.execute("BEGIN")  # Keep memoized proofs on one canonical revision.
            rows = db.execute("SELECT body FROM records WHERE " + " AND ".join(clauses) +  # nosec B608
                              " ORDER BY id LIMIT 2001", parameters).fetchall()
            fp.count(len(rows), 0, 2000, "UniMem scoped runtime search namespace")
            admitted = []
            grounding = GroundingState()
            for row in rows:
                record = MemoryRecord(**json.loads(row[0]))
                if (visible(record, scope, statuses=statuses, at=at)
                        and self._grounded(db, record, scope, statuses=statuses, at=at, grounding=grounding)):
                    admitted.append(record)
        tokens = list(dict.fromkeys(terms(query)))[:32]
        if not tokens:
            return admitted[:limit]
        # Hidden documents never contribute to term frequencies or ranking.
        scores = _bm25(tokens, [terms(json.dumps(record.payload, sort_keys=True)) for record in admitted])
        ranked = sorted(zip(scores, admitted), key=lambda pair: (-pair[0], pair[1].id))
        return [record for score, record in ranked if score > 0][:limit]

    def _authorize_forget(self, db, identity, scope, exact_scope):
        from commontrace import capability_io, memory_control

        row = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
        if row is not None:
            record = MemoryRecord(**json.loads(row[0]))
            actual, descriptor = record.scope, capability_io.runtime_descriptor(record)
        else:
            capture = db.execute("SELECT event,body,scope,session FROM captures WHERE id=?", (identity,)).fetchone()
            if capture is None:
                raise ValueError("record not found")
            actual = json.loads(capture[2])
            descriptor = capability_io.capture_descriptor(identity, capture[3], capture[0],
                                                         json.loads(capture[1]), actual)
        required = memory_control.REQUIRED_PRINCIPAL_SCOPE.get()
        if (scope is None or exact_scope and actual != scope
                or not all(scope.get(key) == value for key, value in actual.items() if key != "labels")
                or not set(actual.get("labels", [])) <= set(scope.get("labels", []))
                or required and actual.get("agent") != required.partition(":")[2]):
            raise PermissionError("UniMem delegated erasure is outside authenticated ownership scope")
        capability_io.assert_forget(self.root, descriptor, namespace="records")

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
        if self._delegated():
            from commontrace import capability_io

            record = capability_io.runtime_prepare(self.root, record)
        from commontrace import capability_io, policy_io

        policy_io.assert_write(self.root, capability_io.runtime_descriptor(record))
        shadow = db.execute("SELECT scope FROM captures WHERE id=?", (record.id,)).fetchone()
        if shadow and (capabilities.CURRENT.get() is not None or json.loads(shadow[0]) != record.scope):
            raise PermissionError("UniMem record identity cannot shadow a capture in another scope or delegation")
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
        if capabilities.CURRENT.get() is not None:
            from commontrace import capability_io

            self._queue_authority(db, capability_io.runtime_attest(self.root, record))
        body = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
        db.execute("INSERT INTO record_events VALUES(?,?,?)", (record.id, record.version, body))
        db.execute(
            "INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (record.id, record.kind, body),
        )
        self._stage_change(db, "record", record.id, record.scope, str(record.version),
                           "assert" if record.status == "active" else "retract")
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
            if source.id != record.id and visible(source, record.scope) and self._runtime_readable(source):
                sources.append(source)
                if len(sources) == 8:
                    break
        if visible(record, record.scope) and self._runtime_readable(record):
            sources.append(record)
        if not sources:
            return  # existing revision-bound observation is withheld by grounding
        observation = facet_observation(entity, facet, sources)
        from commontrace import capability_io, policy_io

        if not policy_io.permits(self.root, capability_io.runtime_descriptor(observation), action="write"):
            return
        prior = db.execute("SELECT body FROM records WHERE id=?", (observation.id,)).fetchone()
        if prior:
            observation = facet_observation(entity, facet, sources, previous=MemoryRecord(**json.loads(prior[0])))
        if self._delegated():
            from commontrace import capability_io

            if not capability_io.permits(self.root, capability_io.runtime_descriptor(observation),
                                         namespace="records", operation="write"):
                return
        self._put(db, observation)

    def history(self, identity: str) -> list[MemoryRecord]:
        self._drain_authority()
        with self.connect() as db:
            rows = db.execute("SELECT body FROM record_events WHERE id=? ORDER BY version", (identity,)).fetchall()
        records = [replace(MemoryRecord(**json.loads(row[0])), is_latest=i == len(rows) - 1)
                   for i, row in enumerate(rows)]
        return [record for record in records if self._runtime_readable(record)]

    def latest(self, identity: str) -> MemoryRecord | None:
        """Indexed revision pointer for writers; readers use grounded current/search."""
        self._drain_authority()
        with self.connect() as db:
            row = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
        record = MemoryRecord(**json.loads(row[0])) if row else None
        return record if record is None or self._runtime_readable(record) else None

    def current(self, identity: str, *, scope: dict, statuses=("active",), at=None) -> MemoryRecord | None:
        """Indexed canonical lookup with the same evidence checks as search."""
        self._drain_authority()
        at = at or now()
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
            if row is None:
                return None
            record = MemoryRecord(**json.loads(row[0]))
            if not visible(record, scope, statuses=statuses, at=at):
                return None
            return record if self._grounded(db, record, scope, statuses=statuses, at=at) else None

    def _grounded(self, db, record, scope, *, statuses, at, seen=frozenset(), delegated=None, grounding=None):
        grounding = GroundingState() if grounding is None else grounding
        cached = grounding.enter(record.id, seen)
        if cached is not None:
            return cached
        eligible = self._grounded_record(db, record, scope, statuses=statuses, at=at,
                                         seen=seen | {record.id}, delegated=delegated, grounding=grounding)
        return grounding.remember(record.id, seen, eligible)

    def _grounded_record(self, db, record, scope, *, statuses, at, seen, delegated, grounding):
        if not self._runtime_readable(record):
            return False
        from commontrace import capability_io, policy_io, policy_relations, purpose, subject_policy

        delegated = self._delegated() if delegated is None else delegated
        if (delegated or policy_io.enabled(self.root) or purpose.enabled(self.root) or subject_policy.enabled(self.root)
                or policy_relations.enabled(self.root, "records")):
            for identity in record.relations.get("caused_by", []):
                capture = db.execute("SELECT session,event,body,scope FROM captures WHERE id=?",
                                     (identity,)).fetchone()
                if capture is None or json.loads(capture[3]) != record.scope:
                    return False
                descriptor = capability_io.capture_descriptor(identity, capture[0], capture[1], json.loads(capture[2]),
                                                              record.scope)
                if not policy_io.permits(self.root, descriptor):
                    return False
                if identity not in policy_relations.authorized_ids(self.root, [identity], "records"):
                    return False
                if delegated and (not capability_io.permits(self.root, descriptor, namespace="records")
                        or capability_io.quarantined(self.root, capability_io.runtime_identity(
                            self.root, identity, capture=True))):
                    return False
        if record.provenance.get("code_evidence"):
            from commontrace.record_visibility import code_evidence_current

            if not code_evidence_current(self.root, record.provenance["code_evidence"]):
                return False
        for source in dict.fromkeys(record.relations.get("derives", [])):
            if grounding.remaining <= 0:
                return False
            row = db.execute("SELECT body FROM records WHERE id=?", (source,)).fetchone()
            if row is None:
                return False
            parent = MemoryRecord(**json.loads(row[0]))
            versions = record.provenance.get("source_versions")
            if versions is not None and (not isinstance(versions, dict) or versions.get(source) != parent.version):
                return False
            if not visible(parent, scope, statuses=("active",), at=at) or not self._grounded(
                db, parent, scope, statuses=statuses, at=at, seen=seen, grounding=grounding,
                delegated=delegated,
            ):
                return False
        return True

    def search(
        self, query: str, *, scope: dict | None = None, kinds=(), statuses=("active",), limit=20, at=None, always=False
    ) -> list[MemoryRecord]:
        self._drain_authority()
        at = at or now()
        from commontrace.search_recipes import terms

        if limit <= 0:
            return []
        from commontrace import policy_io, policy_relations, purpose, subject_policy

        if (self._delegated() or policy_io.enabled(self.root) or purpose.enabled(self.root)
                or subject_policy.enabled(self.root)
                or policy_relations.enabled(self.root, "records")):
            with policy_relations.admission(self.root, "records"):
                return self._delegated_search(query, scope=scope or {}, kinds=kinds, statuses=statuses,
                                              limit=limit, at=at, always=always)
        tokens = list(dict.fromkeys(terms(query)))[:32]
        with self.connect() as db:
            db.execute("BEGIN")
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
            grounding = GroundingState()
            for row in rows:
                record = MemoryRecord(**json.loads(row[0]))
                if (not kinds or record.kind in kinds) and visible(record, scope or {}, statuses=statuses, at=at):
                    if not self._grounded(db, record, scope or {}, statuses=statuses, at=at, delegated=False,
                                          grounding=grounding):
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
        if self._delegated():
            from commontrace import capability_io
            from commontrace.record_visibility import scope_from_labels

            labels = capability_io.inherit_labels(self.root, capability_io.scope_labels(scope))
            if set(labels) != set(capability_io.scope_labels(scope)):
                scope = scope_from_labels(labels)
            capability_io.before_write(self.root, capability_io.capture_descriptor(identity, session, event,
                                                                                   payload, scope), namespace="records")
        from commontrace import capability_io, policy_io

        policy_io.assert_write(self.root, capability_io.capture_descriptor(identity, session, event, payload, scope))
        with self.connect(write=True) as db:
            shadow = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
            if shadow and (capabilities.CURRENT.get() is not None or json.loads(shadow[0])["scope"] != scope):
                raise PermissionError("UniMem capture identity cannot shadow a record in another scope or delegation")
            for row in db.execute(
                "SELECT r.body FROM links l JOIN records r ON r.id=l.derived WHERE l.source=?", (identity,)
            ):
                if json.loads(row[0])["scope"] != scope:
                    raise ValueError("forward capture evidence cannot cross scopes")
            inserted = db.execute(
                "INSERT OR IGNORE INTO captures(id,session,event,body,scope) VALUES(?,?,?,?,?)",
                (identity, session, event, json.dumps(payload), json.dumps(scope, sort_keys=True)),
            ).rowcount
            row = db.execute(
                "SELECT sequence,session,event,body,scope FROM captures WHERE id=?", (identity,)
            ).fetchone()
            if row[1] != session or json.loads(row[4]) != scope or row[2] != event or json.loads(row[3]) != payload:
                raise ValueError("capture identity belongs to another session")
            if inserted:
                if capabilities.CURRENT.get() is not None:
                    from commontrace import capability_io

                    self._queue_authority(db, capability_io.capture_attest(self.root, identity, session, event,
                                                                           payload, scope))
                self._stage_change(db, "capture", identity, scope, str(row[0]), "assert")
            return {"id": identity, "sequence": row[0], "event": row[2], "payload": json.loads(row[3]), "scope": scope}

    def buffer(self, session: str, *, scope: dict, limit=128) -> list[dict]:
        from commontrace import policy_relations

        with policy_relations.admission(self.root, "records"):
            return self._buffer(session, scope=scope, limit=limit)

    def _buffer(self, session: str, *, scope: dict, limit=128) -> list[dict]:
        self._drain_authority()
        through = self._cursor_through(session, scope)
        from commontrace import capability_io, policy_io, policy_relations, purpose, subject_policy

        descriptor = capability_io.capture_descriptor("scope-check", session, "buffer", {}, scope)
        if not policy_io.permits(self.root, descriptor):
            return []
        if (self._delegated() or policy_relations.enabled(self.root, "records")
                or purpose.enabled(self.root) or subject_policy.enabled(self.root)):
            from commontrace import capability_io
            from commontrace import frontier_protocol as fp

            fp.count(limit, 1, 128)
            descriptor = capability_io.capture_descriptor("scope-check", session, "buffer", {}, scope)
            if not capability_io.permits(self.root, descriptor, namespace="records"):
                return []
            with self.connect() as db:
                rows = db.execute("SELECT sequence,id,event,body FROM captures WHERE session=? AND scope=? "
                                  "AND sequence>? ORDER BY sequence LIMIT 2001",
                                  (session, json.dumps(scope, sort_keys=True), through)).fetchall()
            fp.count(len(rows), 0, 2000, "UniMem scoped capture buffer namespace")
            allowed = policy_relations.authorized_ids(self.root, [row[1] for row in rows], "records")
            return [{"sequence": row[0], "id": row[1], "event": row[2], "payload": json.loads(row[3])}
                    for row in rows if row[1] in allowed
                    and policy_io.permits(self.root, capability_io.capture_descriptor(
                        row[1], session, row[2], json.loads(row[3]), scope))
                    and not capability_io.quarantined(self.root, capability_io.runtime_identity(
                        self.root, row[1], capture=True))][:limit]
        with self.connect() as db:
            return [
                {"sequence": row[0], "id": row[1], "event": row[2], "payload": json.loads(row[3])}
                for row in db.execute(
                    "SELECT sequence,id,event,body FROM captures WHERE session=? AND scope=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (session, json.dumps(scope, sort_keys=True), through, limit),
                )
            ]

    def _cursor_through(self, session: str, scope: dict) -> int:
        scope_key = json.dumps(scope, sort_keys=True)
        with self.connect() as db:
            scoped = db.execute(
                "SELECT through FROM scoped_cursors WHERE session=? AND scope=?", (session, scope_key)
            ).fetchone()
            legacy = db.execute("SELECT through FROM cursors WHERE session=?", (session,)).fetchone()
        if legacy is None:
            return int(scoped[0]) if scoped else 0

        # Legacy cursors were keyed only by session. Attribute the old watermark
        # to the scope of the capture it actually sealed; other scopes replay
        # from zero rather than losing interleaved work.
        with self.connect(write=True) as db:
            legacy = db.execute("SELECT through FROM cursors WHERE session=?", (session,)).fetchone()
            if legacy:
                through = int(legacy[0])
                owner = db.execute(
                    "SELECT scope FROM captures WHERE session=? AND sequence=?", (session, through)
                ).fetchone()
                if owner:
                    db.execute(
                        "INSERT INTO scoped_cursors(session,scope,through) VALUES(?,?,?) "
                        "ON CONFLICT(session,scope) DO UPDATE SET through=MAX(through,excluded.through)",
                        (session, owner[0], through),
                    )
                db.execute("DELETE FROM cursors WHERE session=?", (session,))
            scoped = db.execute(
                "SELECT through FROM scoped_cursors WHERE session=? AND scope=?", (session, scope_key)
            ).fetchone()
            return int(scoped[0]) if scoped else 0

    def seal(self, session: str, through: int, *, scope: dict | None = None):
        with self.connect(write=True) as db:
            if scope is None:
                owner = db.execute(
                    "SELECT scope FROM captures WHERE session=? AND sequence=?", (session, through)
                ).fetchone()
                scope = json.loads(owner[0]) if owner else None
            if scope is None:
                db.execute(
                    "INSERT INTO cursors VALUES(?,?) ON CONFLICT(session) DO UPDATE SET through="
                    "MAX(through,excluded.through)",
                    (session, through),
                )
            else:
                scope_key = json.dumps(scope, sort_keys=True)
                owner = db.execute(
                    "SELECT scope FROM captures WHERE session=? AND sequence=?", (session, through)
                ).fetchone()
                if owner and owner[0] != scope_key:
                    raise ValueError("cursor scope must match the sealed capture")
                db.execute(
                    "INSERT INTO scoped_cursors(session,scope,through) VALUES(?,?,?) "
                    "ON CONFLICT(session,scope) DO UPDATE SET through=MAX(through,excluded.through)",
                    (session, scope_key, through),
                )

    def closed_boundary(self, session: str, *, scope: dict) -> dict | None:
        through = self._cursor_through(session, scope)
        with self.connect() as db:
            row = db.execute(
                "SELECT sequence,id,event,body FROM captures WHERE session=? AND scope=? "
                "AND event IN ('Stop','SessionEnd','SubagentStop') AND sequence>? "
                "ORDER BY sequence LIMIT 1",
                (session, json.dumps(scope, sort_keys=True), through),
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

    def forget(self, identity: str, *, reason: str, expected_version: int | None = None,
               scope: dict | None = None, exact_scope: bool = False, _authorized_cascade: bool = False) -> dict:
        from commontrace import incident

        with incident.protect(self.root):
            return self._forget(identity, reason=reason, expected_version=expected_version, scope=scope,
                                exact_scope=exact_scope, _authorized_cascade=_authorized_cascade)

    def _forget(self, identity: str, *, reason: str, expected_version: int | None = None,
               scope: dict | None = None, exact_scope: bool = False, _authorized_cascade: bool = False) -> dict:
        if not reason.strip():
            raise ValueError("erasure requires a reason")
        with self.connect(write=True) as db:
            if (capabilities.CURRENT.get() is not None or scope is not None) and not _authorized_cascade:
                self._authorize_forget(db, identity, scope, exact_scope)
            if _authorized_cascade and capabilities.CURRENT.get() is not None:
                from commontrace import memory_authority
                from commontrace.memory_record import atomic_fact_from_record

                # A canonical fact id is not proof that an arbitrary native
                # row with that id belongs to the fact. Require its authentic
                # bridge receipt before widening a delegated privacy cascade.
                mirrored = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
                valid = False
                if mirrored is not None:
                    try:
                        fact = atomic_fact_from_record(MemoryRecord(**json.loads(mirrored[0])))
                        valid = fact.id == identity and memory_authority.verify(
                            self.root, fact.origin, memory_authority.fact_record(fact))
                    except (ValueError, TypeError, KeyError):
                        valid = False
                if not valid:
                    return {"at": now(), "deleted": [], "unverified_cascade": [identity],
                            "reason": "no authenticated native mirror of the authorized canonical fact"}
            db.execute("PRAGMA secure_delete=ON")
            if expected_version is not None:
                current = db.execute("SELECT body FROM records WHERE id=?", (identity,)).fetchone()
                if current is None or json.loads(current[0])["version"] != expected_version:
                    raise ValueError("expiration record version changed")
            direct_capture = db.execute("SELECT scope FROM captures WHERE id=?", (identity,)).fetchone()
            pending, deleted = [identity], set()
            capture_ids = {(identity, direct_capture[0])} if direct_capture else set()
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
            from commontrace import incident, memory_authority, nextwave_store, safety

            if scope is not None and not _authorized_cascade:
                for _, capture_scope in capture_ids:
                    actual = json.loads(capture_scope)
                    if (exact_scope and actual != scope
                            or not all(scope.get(key) == value for key, value in actual.items() if key != "labels")
                            or not set(actual.get("labels", [])) <= set(scope.get("labels", []))):
                        raise PermissionError("UniMem capture erasure would cross the authenticated ownership scope")
            if capabilities.CURRENT.get() is not None:
                from commontrace import capability_io

                # A fact-only leaf must not gain permission to erase a hidden
                # ancestor capture merely by naming it as its own evidence.
                for capture_id, capture_scope in capture_ids:
                    source = db.execute("SELECT session,event,body FROM captures WHERE id=? AND scope=?",
                                        (capture_id, capture_scope)).fetchone()
                    if source is not None:
                        descriptor = capability_io.capture_descriptor(capture_id, source[0], source[1],
                            json.loads(source[2]), json.loads(capture_scope))
                        capability_io.assert_forget(self.root, descriptor, namespace="records")
            native_ids = sorted(deleted | {value[0] for value in capture_ids})
            references = [row[0] for row in db.execute(
                "SELECT ref FROM resource_refs WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(native_ids),))]
            if references:
                # Governed references carry the namespace, so a native id that
                # matches a fact id cannot erase the unrelated fact's artifacts.
                incident.assert_unpinned(self.root, references)
                safety.assert_mutable(self.root, references)
                for reference in references:
                    memory_authority.record_forgetting(self.root, reference, forgotten=True)
                nextwave_store._erase_sources(self.root, references)
            for capture_id, scope in capture_ids:
                if db.execute("SELECT 1 FROM captures WHERE id=? AND scope=?", (capture_id, scope)).fetchone():
                    self._stage_change(db, "capture", capture_id, json.loads(scope), "", "erase")
                    if self._delegated():
                        from commontrace import capability_io, memory_authority

                        memory_authority.record_forgetting(self.root, capability_io.runtime_identity(
                            self.root, capture_id, capture=True), forgotten=True)
                db.execute("DELETE FROM resource_refs WHERE id=? AND kind='capture'", (capture_id,))
                db.execute("DELETE FROM captures WHERE id=? AND scope=?", (capture_id, scope))
            for token, body in db.execute("SELECT token,body FROM deliveries").fetchall():
                receipt = json.loads(body)
                assigned = {row.get("lesson") for row in receipt["assignments"]}
                if (set(receipt["ids"]) | assigned) & deleted:
                    db.execute("DELETE FROM deliveries WHERE token=?", (token,))
            for current in deleted:
                row = db.execute("SELECT body FROM records WHERE id=?", (current,)).fetchone()
                if row:
                    self._stage_change(db, "record", current, json.loads(row[0])["scope"], "", "erase")
                    if self._delegated():
                        from commontrace import capability_io, memory_authority

                        memory_authority.record_forgetting(self.root, capability_io.runtime_identity(
                            self.root, current), forgotten=True)
                db.execute("DELETE FROM resource_refs WHERE id=? AND kind='record'", (current,))
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
        if capabilities.CURRENT.get() is not None:
            return {**audit, "deleted": [identity] if identity in audit["deleted"] else []}
        return audit
