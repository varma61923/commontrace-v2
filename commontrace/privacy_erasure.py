"""Explicit local erasure: revoke first, cascade derivatives, remove payload history.

External source files, database replicas, third-party mirrors and backups require
operator erasure at their own boundaries. The result enumerates local surfaces;
it never labels a local operation as verified remote erasure.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3

from commontrace import _jsonl, fact_embeddings, fact_store, hierarchical, memory_authority, memory_guard, paths
from commontrace.memory_journal import Journal, now


def purge_fact(root: str, identity: str, *, reason: str) -> dict:
    from commontrace import incident

    with incident.protect(root):
        return _purge_fact(root, identity, reason=reason)


def _purge_fact(root: str, identity: str, *, reason: str) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("hard erasure requires a reason")
    facts = hierarchical.load_facts(root)
    if identity not in facts:
        raise ValueError("fact not found")
    deleted = {identity}
    while True:
        derived = {fact.id for fact in facts.values() if any(e.source.id in deleted for e in fact.evidence)}
        derived.update(facts[key].superseded_by for key in deleted if facts[key].superseded_by in facts)
        if derived <= deleted:
            break
        deleted |= derived
    for key in deleted:
        from commontrace import incident

        incident.assert_unpinned(root, [key])
    for key in deleted:
        memory_authority.record_forgetting(root, key, forgotten=True)
    digests = {hashlib.sha256(facts[key].statement.encode()).hexdigest() for key in deleted}
    if fact_store.enabled(root):
        with fact_store.transaction(root, write=True) as canonical:
            canonical.connection.execute("PRAGMA secure_delete=ON")
            for key in deleted:
                fact_store._remove_projection(canonical.connection, key)
                canonical.connection.execute("DELETE FROM events WHERE fact_id=?", (key,))
            canonical.connection.execute("DELETE FROM lexicon WHERE documents=0")
            canonical.connection.execute("INSERT INTO fact_fts(fact_fts) VALUES('rebuild')")
        fact_store.close_connections(root)
        db = sqlite3.connect(fact_store.database_path(root), isolation_level=None)
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("VACUUM")
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("erasure checkpoint blocked by a reader; close readers and retry")
        finally:
            db.close()
    else:
        with hierarchical.mutate_facts(root) as canonical:
            for key in deleted:
                canonical.pop(key, None)
    directory = os.path.join(paths.memory_dir(root), "facts")
    for filename in os.listdir(directory):
        if filename.startswith("embeddings-") and filename.endswith(".db"):
            db = sqlite3.connect(os.path.join(directory, filename))
            try:
                db.execute("PRAGMA secure_delete=ON")
                for digest in digests:
                    db.execute("DELETE FROM vec WHERE hash=?", (digest,))
                db.commit()
                db.execute("VACUUM")
            finally:
                db.close()
    vector_path = os.path.join(directory, "vectors.sqlite3")
    if os.path.isfile(vector_path):
        with _jsonl.locked(vector_path + ".projection"):
            db = sqlite3.connect(vector_path)
            try:
                db.execute("PRAGMA secure_delete=ON")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("commontrace_vectors", "commontrace_snapshot_stage", "commontrace_snapshot_versions"):
                    if table in tables:
                        db.executemany("DELETE FROM " + table + " WHERE key=?", [(key,) for key in deleted])  # nosec B608
                db.commit()
                db.execute("VACUUM")
                if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise RuntimeError("vector erasure checkpoint blocked; close readers and retry")
            finally:
                db.close()
    mirror_deleted = _purge_mirrors(root, deleted)
    with fact_embeddings._LOCK:
        for key in list(fact_embeddings._VECTORS):
            if key[1] in digests:
                del fact_embeddings._VECTORS[key]
        fact_embeddings._ARRAYS.clear()
    source = hierarchical._facts_file(root)
    if fact_store.enabled(root) and os.path.isfile(source):
        with _jsonl.locked(source):
            _jsonl.write_rows(source, [row for row in _jsonl.read_rows(source) if row.get("id") not in deleted])
    origins = os.path.join(paths.memory_dir(root), "origins.jsonl")
    with _jsonl.locked(origins):
        rows = _jsonl.read_rows(origins)
        if rows:
            _jsonl.write_rows(origins, [row for row in rows if row.get("record", {}).get("id") not in deleted])
    journal_deleted = set()
    journal = Journal(root)
    for key in deleted:
        journal_deleted.update(journal.forget(key, reason=reason)["deleted"])
    from commontrace import fact_index

    fact_index.clear_cache()
    from commontrace import kg_similarity

    kg_similarity._QUERY_VECTORS.clear()
    result = {
        "at": now(),
        "deleted_facts": sorted(deleted),
        "deleted_runtime_records": sorted(journal_deleted),
        "deleted_derived_mirrors": sorted(mirror_deleted),
        "reason": memory_guard.sanitize_metadata({"reason": reason}, pii=True)[0]["reason"],
        "erased_surfaces": [
            "canonical fact events",
            "FTS postings",
            "fact vectors and snapshots",
            "runtime records and captures",
            "observations",
            "control revisions",
            "graph mirrors",
        ],
        "remote_erasure": False,
        "source_and_backup_erasure": "operator required",
    }
    audit = os.path.join(paths.memory_dir(root), "erasure-audit.jsonl")
    with _jsonl.locked(audit):
        _jsonl.append_row(audit, result)
    return result


def _purge_mirrors(root: str, deleted: set[str]) -> set[str]:
    """Remove structured descendants, including every historical control revision."""
    from commontrace import frontmatter, graph, memory_control, observations, retention

    retention.erase_rewrites(root, deleted)

    derived = set(deleted)
    revisions = []
    for kind in memory_control.KINDS:
        directory = memory_control._directory(root, kind)
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                if name.endswith(".md"):
                    filename = os.path.join(directory, name)
                    metadata, _ = frontmatter.read(filename)
                    revisions.append((filename, metadata))
    observation_path = observations._observations_file(root)
    observed = _jsonl.read_rows(observation_path)
    while True:
        additions = {row.get("id") for row in observed if set(row.get("source_fact_ids", [])) & derived}
        additions.update(row.get("id") for _, row in revisions if set(row.get("data", {}).get("sources", [])) & derived)
        additions.discard(None)
        if additions <= derived:
            break
        derived.update(additions)
    for filename, row in revisions:
        if row.get("id") in derived:
            with _jsonl.locked(os.path.dirname(filename)):
                os.unlink(filename)
    files = [
        (observation_path, lambda row: row.get("id") in derived),
        (
            os.path.join(paths.memory_dir(root), "facts", "conflicts.jsonl"),
            lambda row: row.get("older") in derived or row.get("newer") in derived,
        ),
        (os.path.join(paths.memory_dir(root), "origins.jsonl"), lambda row: row.get("record", {}).get("id") in derived),
    ]
    for filename, affected in files:
        if os.path.isfile(filename):
            with _jsonl.locked(filename):
                _jsonl.write_rows(filename, [row for row in _jsonl.read_rows(filename) if not affected(row)])
    node_ids = derived | {"memory:" + key for key in derived}
    with _jsonl.locked(graph._lock_file(root)):
        nodes = _jsonl.read_rows(graph._nodes_file(root))
        node_ids.update(row["id"] for row in nodes if row.get("properties", {}).get("fact_id") in derived)
        if nodes:
            _jsonl.write_rows(graph._nodes_file(root), [row for row in nodes if row["id"] not in node_ids])
        edges = _jsonl.read_rows(graph._edges_file(root))
        if edges:
            _jsonl.write_rows(
                graph._edges_file(root),
                [row for row in edges if row.get("source") not in node_ids and row.get("target") not in node_ids],
            )
    return derived - deleted
