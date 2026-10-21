"""Keep the knowledge graph in step with the fact store, automatically.

Every active fact becomes a ``memory:<fact id>`` node linked by ``mentions`` edges to
the entities it names (typed by `ontology_classify`, consistent with how the store
has typed them before), and the relations it states become typed edges
(`relation_extraction`; the offline patterns, or an LLM when one is passed). Every
graph write carries provenance naming the fact.

The projection is incremental and idempotent: `sync` processes only facts that are new
or whose statement or scopes changed since the last pass (recorded in
``memory/graph/fact_sync.json``), and when a fact stops being current (superseded,
invalidated, deleted, forgotten or expired) the edges it produced are closed at the
time it ended, so graph history and fact history agree (bi-temporal on both sides).

Structured contradictions: when a fact asserts an exclusive relation (one value at
a time: ``works_at``, ``located_in``, ``owned_by``, ``status`` and any relation the
ontology marks exclusive) that ends an earlier fact's value, the two facts are
recorded as a contradiction of kind ``exclusive`` ("Alice works at Acme" against
"Alice works at Globex" share no slot words, so the lexical detector cannot see it).
Under ``UNIMEM_FACT_CONFLICTS=supersede`` the earlier fact is superseded through
the same temporal guard as `hierarchical.resolve_contradiction`; otherwise the pair
waits for review in `fact_conflicts.pending`.

It runs on its own: the daemon's consolidation pass and the hook broker call
`run_if_due`, a cheap check that only syncs when the fact store changed. The
``UNIMEM_GRAPH_AUTO=off`` switch turns the automatic runs off;
``unimem graph sync`` runs a pass on demand."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from unimem import paths
from unimem.configuration import getenv as _configuration_getenv

ENV = "UNIMEM_GRAPH_AUTO"
SOURCE = "graph_sync"
DEFAULT_LIMIT = 2000
_LOCK = threading.Lock()
_SYNCED: dict[str, list[int]] = {}  # root -> fact-store stamp of its last completed pass


def enabled() -> bool:
    value = (_configuration_getenv(ENV, "on") or "on").strip().lower()
    if value not in ("on", "off", "1", "0", "true", "false"):
        raise ValueError(f"{ENV} must be on or off")
    return value in ("on", "1", "true")


def state_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "graph", "fact_sync.json")


def _load_state(root: str) -> dict[str, Any]:
    try:
        with open(state_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"version": 1, "facts": {}, "stamp": None}
    if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
        return {"version": 1, "facts": {}, "stamp": None}
    return data


def _save_state(root: str, state: dict[str, Any]) -> None:
    path = state_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, sort_keys=True)
    os.replace(tmp, path)


def _store_stamp(root: str) -> list[int]:
    """A cheap change marker for the fact store: size and mtime of its files."""
    from unimem import fact_store, hierarchical

    stamp: list[int] = []
    candidates = [hierarchical._facts_file(root)]
    if fact_store.enabled(root):
        base = fact_store.database_path(root)
        candidates = [base, base + "-wal"]
    for path in candidates:
        try:
            st = os.stat(path)
            stamp += [int(st.st_size), int(st.st_mtime_ns)]
        except OSError:
            stamp += [0, 0]
    return stamp


def _digest(fact) -> str:
    text = json.dumps([fact.statement, sorted(fact.scopes or []), fact.valid_from or ""], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _current(fact, now: datetime) -> bool:
    from unimem import hierarchical

    return fact.status == "active" and not fact.forgotten and hierarchical._valid_at(fact, now) and \
        not (fact.expires_at and fact.expires_at <= now.isoformat())


def fact_node(fact_id: str) -> str:
    return f"memory:{fact_id}"


def plan(root: str) -> tuple[list[Any], list[tuple[str, str]], dict[str, Any]]:
    """(facts to project, (retired fact id, when it ended) pairs, the current state)."""
    from unimem import hierarchical

    state = _load_state(root)
    seen: dict[str, str] = state["facts"]
    now = datetime.now(timezone.utc)
    facts = hierarchical.load_facts(root)
    todo, retired = [], []
    for fact in sorted(facts.values(), key=lambda f: (f.created_at or "", f.id)):
        if _current(fact, now):
            if seen.get(fact.id) != _digest(fact):
                todo.append(fact)
        elif fact.id in seen:
            ended = fact.valid_until or fact.retracted_at or fact.expires_at or now.isoformat()
            retired.append((fact.id, ended))
    for fact_id in set(seen) - set(facts):
        retired.append((fact_id, now.isoformat()))  # removed outright (erasure)
    return todo, retired, state


def _close_edges_from(txn, node_id: str, at: str, reason: str, *, keep: set[str] | None = None) -> int:
    from unimem import graph

    closed = 0
    for edge in list(txn.by_entity.get(node_id, [])):
        if edge.invalid_at is not None or edge.source != node_id or (keep is not None and edge.target in keep):
            continue
        graph._close(edge, at, graph._now(), reason)
        txn.edges_dirty = True
        closed += 1
    return closed


def _relations_by_fact(txn) -> dict[str, list]:
    """Open relation edges a fact's statement produced, by ``fact:<id>`` source: one
    pass over the edges per sync, not one per retired fact."""
    out: dict[str, list] = {}
    for edge in txn.edges:
        props = edge.properties or {}
        source = props.get("source", "")
        if edge.invalid_at is None and isinstance(source, str) and source.startswith("fact:") and \
                str(props.get("extracted_by", "")).startswith("relation_extraction"):
            out.setdefault(source, []).append(edge)
    return out


def _close_relations_from(txn, index: dict[str, list], fact_id: str, at: str, reason: str) -> int:
    from unimem import graph

    edges = index.pop(f"fact:{fact_id}", [])
    for edge in edges:
        graph._close(edge, at, graph._now(), reason)
    if edges:
        txn.edges_dirty = True
    return len(edges)


def sync(root: str, *, limit: int = DEFAULT_LIMIT, llm=None) -> dict[str, Any]:
    """Project new and changed facts into the graph and close what retired facts produced."""
    from unimem import entities, graph, ontology, ontology_classify, relation_extraction

    with _LOCK:
        stamp = _store_stamp(root)
        todo, retired, state = plan(root)
        todo = todo[:max(1, int(limit))]
        report = {"facts": len(todo), "retired": len(retired), "entities": 0, "new_entities": 0,
                  "mentions": 0, "relations": 0, "closed": 0, "conflicts": 0, "errors": []}
        if not todo and not retired:
            state["stamp"] = stamp
            _save_state(root, state)
            _SYNCED[os.path.abspath(root)] = stamp
            return report
        onto = ontology.load(root)
        exemplars = ontology_classify.Exemplars.from_graph(root)
        mentions = entities.extract_batch([f.statement for f in todo], onto=onto, use_spacy=False,
                                          exemplars=exemplars)
        triples: list[tuple[Any, list]] = []
        for fact in todo:
            try:
                found = relation_extraction.extract(fact.statement, onto, llm=llm, exemplars=exemplars)
            except ValueError as exc:
                report["errors"].append(f"{fact.id}: {exc}")
                found = []
            triples.append((fact, [dataclasses.replace(t, valid_at=t.valid_at or fact.valid_from or None)
                                   for t in found]))
        seen: dict[str, str] = state["facts"]
        exclusive = {name for name, rel in onto.relations.items() if rel.exclusive}
        replaced: list[tuple[str, str, str]] = []  # (older fact, newer fact, relation)
        with graph.batch(root) as txn:
            relations = _relations_by_fact(txn)
            watched = [e for e in txn.edges if e.invalid_at is None and e.relation in exclusive and
                       str((e.properties or {}).get("source", "")).startswith("fact:")]
            for fact_id, ended in retired:
                node = fact_node(fact_id)
                report["closed"] += _close_edges_from(txn, node, ended, "fact no longer current")
                report["closed"] += _close_relations_from(txn, relations, fact_id, ended, "fact no longer current")
                seen.pop(fact_id, None)
            for fact, found in zip(todo, mentions):
                node = fact_node(fact.id)
                prov = {"source_path": f"fact:{fact.id}", "run_id": SOURCE, "detail": {"method": "graph_sync"}}
                graph._put_node(txn, node, "memory", fact.statement[:120],
                                {"fact_id": fact.id, "scopes": list(fact.scopes or []), "source": SOURCE}, prov)
                keep = set()
                for m in found:
                    existing = txn.nodes.get(m.key)
                    if existing is None:
                        report["new_entities"] += 1
                    aliases = sorted({*((existing.properties or {}).get("aliases", []) if existing else []),
                                      m.name})[:20]
                    count = int(((existing.properties or {}).get("mentions", 0) if existing else 0) or 0)
                    graph._put_node(txn, m.key, m.type, m.name if existing is None else "",
                                    {"aliases": aliases, "mentions": count + 1}, prov if existing is None else None)
                    keep.add(m.key)
                    if not any(e.invalid_at is None for e in txn.by_key.get((node, m.key, "mentions"), [])):
                        graph.add_edge(root, node, m.key, "mentions", valid_at=fact.valid_from or None,
                                       properties={"extracted_by": m.source, "source": f"fact:{fact.id}"},
                                       provenance=prov)
                        report["mentions"] += 1
                report["entities"] += len(keep)
                # A changed statement no longer naming an entity closes that link.
                report["closed"] += _close_edges_from(txn, node, graph._now(), "statement changed", keep=keep)
            for fact, found in triples:
                if seen.get(fact.id) is not None:
                    report["closed"] += _close_relations_from(txn, relations, fact.id, graph._now(),
                                                              "statement changed")
                if found:
                    before = len(txn.edges)
                    written = relation_extraction.write_triples(root, found, source=f"fact:{fact.id}",
                                                                run_id=SOURCE)
                    report["relations"] += written["edges_written"]
                    report["errors"] += written["errors"]
                    if any(t.relation in exclusive for t in found):
                        for edge in watched:
                            reason = str((edge.properties or {}).get("closed_reason", ""))
                            older = str(edge.properties.get("source", ""))[5:]
                            if edge.invalid_at is not None and reason.startswith("superseded by") and \
                                    older != fact.id:
                                replaced.append((older, fact.id, edge.relation))
                        watched = [e for e in watched if e.invalid_at is None]
                    watched += [e for e in txn.edges[before:] if e.invalid_at is None and e.relation in exclusive]
                seen[fact.id] = _digest(fact)
        report["conflicts"] = _record_exclusive(root, replaced, report["errors"])
        state["facts"] = seen
        state["stamp"] = stamp if len(todo) < limit else None
        state["synced_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(root, state)
        if state["stamp"] is not None:
            _SYNCED[os.path.abspath(root)] = stamp
        return report


def _record_exclusive(root: str, replaced: list[tuple[str, str, str]], errors: list[str]) -> int:
    """Record (and under supersede mode, resolve) contradictions that exclusive
    relations exposed; returns how many were recorded."""
    from unimem import fact_conflicts, hierarchical

    if not replaced:
        return 0
    mode = fact_conflicts.mode()
    if mode == "off":
        return 0
    facts = hierarchical.load_facts(root)
    now = datetime.now(timezone.utc).isoformat()
    out: list[fact_conflicts.Conflict] = []
    for older, newer, relation in dict.fromkeys(replaced):
        if older not in facts or newer not in facts or facts[older].status != "active":
            continue
        action, reason = "flagged", f"exclusive relation {relation}"
        if mode == "supersede":
            try:
                hierarchical.resolve_contradiction(root, older, newer)
                action = "superseded"
            except (ValueError, KeyError) as exc:  # the temporal guard refused: different windows
                reason = f"{reason}; not superseded: {exc}"
        out.append(fact_conflicts.Conflict(older, newer, "exclusive", facts[older].statement,
                                           facts[newer].statement, now, action, reason))
    try:
        fact_conflicts.record(root, out)
    except OSError as exc:
        errors.append(f"could not record contradictions: {exc}")
        return 0
    return len(out)


def due(root: str) -> bool:
    """Whether the fact store changed since the last completed pass (two stat calls when
    nothing changed: the last stamp is kept in memory)."""
    key = os.path.abspath(root)
    stamp = _store_stamp(root)
    if _SYNCED.get(key) == stamp:
        return False
    recorded = _load_state(root).get("stamp")
    if recorded == stamp:
        _SYNCED[key] = stamp
        return False
    return True


def run_if_due(root: str, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any] | None:
    """`sync` when automatic projection is on and the fact store changed; else None.
    Never raises: a failed pass is reported and retried on the next call."""
    try:
        if not enabled() or not due(root):
            return None
        return sync(root, limit=limit)
    except Exception as exc:  # noqa: BLE001 - background maintenance must not break its caller
        return {"error": str(exc)}
