"""Optional source-bound graph snapshots; local canonical evidence remains authoritative.

The Neo4j/FalkorDB mirror keeps the local graph's typing and bi-temporality: every
edge carries its ontology relation (as `e.relation`, and as the relationship type
when that name sanitizes safely), its valid time (`valid_at`, `invalid_at`), its
record time (`created_at`, `closed_recorded_at`) and its `expired_at`. Reads apply
`graph._is_active_edge`'s semantics in Cypher, and every value is a parameter."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

GENERIC_TYPE = "MEMORY"
MAX_HOPS = 4
MAX_ROWS = 2000
_REL_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DIRECTIONS = {"out": "-[e]->", "in": "<-[e]-", "both": "-[e]-"}
# Bi-temporal visibility, mirroring graph._is_active_edge: an edge recorded after
# $known is unknown then; a close recorded after $known had not happened; the
# edge must be valid at $at. Fixed-width UTC strings compare chronologically.
ACTIVE_EDGE = ("($known IS NULL OR e.created_at IS NULL OR e.created_at <= $known) AND "
               "(e.valid_at IS NULL OR e.valid_at <= $at) AND "
               "(e.invalid_at IS NULL OR e.invalid_at > $at OR "
               "($known IS NOT NULL AND e.closed_recorded_at IS NOT NULL AND e.closed_recorded_at > $known)) AND "
               "(e.expired_at IS NULL OR e.expired_at > $at)")


def _moment(value=None):
    """A fixed-width UTC timestamp, so string order in the native engine is time order."""
    instant = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _optional_moment(value):
    return _moment(value) if value else None


def relation_key(name) -> str:
    """The canonical relation name stored in `e.relation`: the ontology's key form."""
    key = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not key or len(key) > 64:
        raise ValueError("graph relation names are 1-64 characters")
    return key


def relationship_type(relation: str) -> str:
    """The native relationship type for `relation`. Cypher cannot parameterize a type, so
    only a strict `^[A-Z][A-Z0-9_]{0,63}$` form is used (and backtick-quoted); anything
    else keeps the generic type, with the relation still stored as a property."""
    candidate = str(relation or "").upper()
    return candidate if _REL_TYPE.fullmatch(candidate) else GENERIC_TYPE


def _relations_param(relations) -> list[str] | None:
    if relations is None:
        return None
    values = [relations] if isinstance(relations, str) else list(relations)
    if not values or len(values) > 64:
        raise ValueError("filter by 1-64 relations, or none")
    return sorted({relation_key(v) for v in values})


def visibility(as_of=None, known_at=None) -> dict:
    """`$at`/`$known` for `ACTIVE_EDGE`: as `graph._is_active_edge`, the valid time
    defaults to the record time when only that is given, else to now."""
    known = _optional_moment(known_at)
    return {"at": _moment(as_of) if as_of else known or _moment(), "known": known}


class _SnapshotGraph:
    def __init__(self, namespace: str):
        if not isinstance(namespace, str) or not 1 <= len(namespace) <= 128:
            raise ValueError("bounded operator graph namespace required")
        self.namespace, self.generation = namespace, ""

    def _query(self, statement, params):
        raise NotImplementedError

    def begin_snapshot(self, source_id: str):
        self._ensure_unique_namespace()
        prior = self._query("MERGE (s:CommonTraceIndex {namespace:$ns}) "
                            "ON CREATE SET s.source=$source,s.current='' RETURN s.source,s.current",
                            {"ns": self.namespace, "source": source_id})
        if not prior or prior[0][0] != source_id:
            raise PermissionError("graph namespace is bound to a different canonical source")
        self.expected_head = prior[0][1]
        self.generation = uuid.uuid4().hex
        self.source = source_id

    def _ensure_unique_namespace(self):
        raise NotImplementedError

    def write_node(self, node_id: str, properties: dict):
        if not self.generation:
            raise ValueError("begin a source-bound graph snapshot before writing")
        self._query("MERGE (n:CommonTrace {id:$id,namespace:$ns,generation:$gen}) SET n.properties=$properties",
                    {"id": node_id, "ns": self.namespace, "gen": self.generation, "properties": json.dumps(properties)})

    def write_edge(self, source: str, target: str, *, relation: str = "relates_to", weight: float = 1.,
                   valid_at=None, invalid_at=None, expired_at=None, created_at=None, closed_recorded_at=None,
                   properties: dict | None = None, valid_from=None, valid_until=None):
        """Mirror one bi-temporal edge. The relation is kept as `e.relation` and, when it
        sanitizes safely, as the relationship type; every value is a parameter.
        `valid_from`/`valid_until` are the earlier names of `valid_at`/`invalid_at`."""
        if not self.generation:
            raise ValueError("begin a source-bound graph snapshot before writing")
        relation = relation_key(relation)
        start, end = _optional_moment(valid_at or valid_from), _optional_moment(invalid_at or valid_until)
        expired = _optional_moment(expired_at)
        rel_type = relationship_type(relation)
        self._query("MATCH (a:CommonTrace {id:$source,namespace:$ns,generation:$gen}), "
                    "(b:CommonTrace {id:$target,namespace:$ns,generation:$gen}) "
                    f"CREATE (a)-[e:`{rel_type}`]->(b) SET e.relation=$relation,e.weight=$weight,"
                    "e.valid_at=$start,e.invalid_at=$end,e.expired_at=$expired,e.created_at=$created,"
                    "e.closed_recorded_at=$closed,e.valid_from=$start,e.valid_until=$until,e.properties=$properties",
                    {"source": source, "target": target, "ns": self.namespace, "gen": self.generation,
                     "relation": relation, "weight": weight, "start": start, "end": end, "expired": expired,
                     "created": _optional_moment(created_at), "closed": _optional_moment(closed_recorded_at),
                     "until": min((m for m in (end, expired) if m), default=None),
                     "properties": json.dumps(properties or {}, sort_keys=True, default=str)})

    def publish_snapshot(self):
        if not self.generation:
            raise ValueError("no graph snapshot to publish")
        # Lock the metadata row before comparing its head; concurrent rebuilds
        # cannot replace a newer complete publication with an older staging set.
        result = self._query("MATCH (s:CommonTraceIndex {namespace:$ns}) "
            "SET s.serial=coalesce(s.serial,0)+1 WITH s "
            "WHERE s.current=$expected AND s.source=$source SET s.current=$gen RETURN s.current",
            {"ns": self.namespace, "gen": self.generation, "source": self.source, "expected": self.expected_head})
        if not result or result[0][0] != self.generation:
            self.generation = ""
            raise RuntimeError("graph publication conflicted with another complete snapshot")
        self.generation = ""
        # Historical and staging generations remain invisible. Retaining them
        # avoids deleting another writer's staging generation during publication.

    def _edges_from(self, ids: list[str], *, direction: str, relations, as_of, known_at, limit: int) -> list:
        """Published edges incident to `ids`, visible at (as_of, known_at):
        rows of (here, there, relation, outgoing, weight, valid_at, invalid_at)."""
        if direction not in _DIRECTIONS:
            raise ValueError("direction is out, in or both")
        return self._query("MATCH (s:CommonTraceIndex {namespace:$ns}), "
            f"(n:CommonTrace {{namespace:$ns}}){_DIRECTIONS[direction]}(m:CommonTrace {{namespace:$ns}}) "
            "WHERE n.id IN $ids AND n.generation=s.current AND m.generation=s.current AND "
            "($relations IS NULL OR e.relation IN $relations) AND " + ACTIVE_EDGE + " "
            "RETURN n.id,m.id,coalesce(e.relation,'relates_to'),startNode(e)=n,coalesce(e.weight,1.0),"
            "e.valid_at,e.invalid_at LIMIT $limit",
            {"ns": self.namespace, "ids": list(ids), "relations": _relations_param(relations),
             "limit": int(limit), **visibility(as_of, known_at)})

    def neighbors(self, node_id: str, *, as_of=None, known_at=None) -> list[dict]:
        """Nodes one visible edge away, through any relation: [{neighbor_id, weight}]."""
        rows = self._edges_from([node_id], direction="both", relations=None, as_of=as_of, known_at=known_at,
                                limit=MAX_ROWS)
        return [{"neighbor_id": row[1], "weight": row[4]} for row in rows]

    def typed_neighbors(self, node_id: str, *, relations=None, direction: str = "both", as_of=None,
                        known_at=None) -> list[dict]:
        """Neighbors with the connecting relation and validity, optionally only through
        `relations` and in one `direction` (out, in or both)."""
        rows = self._edges_from([node_id], direction=direction, relations=relations, as_of=as_of,
                                known_at=known_at, limit=MAX_ROWS)
        return [{"neighbor_id": row[1], "relation": row[2], "direction": "out" if row[3] else "in",
                 "weight": row[4], "valid_at": row[5], "invalid_at": row[6]} for row in rows]

    def multi_hop(self, start, max_hops: int = 2, relations=None, as_of=None, known_at=None, *,
                  max_edges: int = MAX_ROWS) -> dict:
        """Breadth-first subgraph within `max_hops` (1-4) of `start` (an id or ids), one
        bounded query per hop, in `graph.multi_hop_subgraph`'s shape and visibility."""
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not 1 <= max_hops <= MAX_HOPS:
            raise ValueError(f"max_hops must be an integer from 1 to {MAX_HOPS}")
        starts = [start] if isinstance(start, str) else list(start)
        if not starts or len(starts) > MAX_ROWS or not all(isinstance(s, str) and s for s in starts):
            raise ValueError(f"multi_hop needs 1-{MAX_ROWS} non-empty start ids")
        cap = max(1, min(int(max_edges), MAX_ROWS))
        distances = dict.fromkeys(starts, 0)
        edges: list[dict] = []
        seen: set[tuple] = set()
        frontier = list(distances)
        for hop in range(1, max_hops + 1):
            if not frontier or len(edges) >= cap:
                break
            rows = self._edges_from(frontier, direction="both", relations=relations, as_of=as_of,
                                    known_at=known_at, limit=cap)
            reached = []
            for here, there, relation, outgoing, weight, start_at, end_at in rows:
                source, target = (here, there) if outgoing else (there, here)
                key = (source, target, relation, start_at)
                if key not in seen and len(edges) < cap:
                    seen.add(key)
                    edges.append({"source": source, "target": target, "relation": relation, "weight": weight,
                                  "valid_at": start_at, "invalid_at": end_at})
                if there not in distances:
                    distances[there] = hop
                    reached.append(there)
            frontier = reached
        return {"nodes": list(distances), "edges": edges, "hop_distances": distances}


class Neo4jGraph(_SnapshotGraph):
    def __init__(self, uri: str, *, username: str, password: str, database: str = "neo4j",
                 namespace: str = "commontrace"):
        super().__init__(namespace)
        try:
            from neo4j import GraphDatabase
        except ImportError:
            raise RuntimeError("Neo4j backend requires commontrace[graph]") from None
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.database = database

    def _query(self, statement, params):
        with self.driver.session(database=self.database) as session:
            return [list(row.values()) for row in session.run(statement, **params)]

    def _ensure_unique_namespace(self):
        self._query("CREATE CONSTRAINT commontrace_namespace IF NOT EXISTS "
                    "FOR (s:CommonTraceIndex) REQUIRE s.namespace IS UNIQUE", {})

    def close(self):
        self.driver.close()


class FalkorGraph(_SnapshotGraph):
    def __init__(self, *, host: str = "localhost", port: int = 6379, password: str | None = None,
                 graph_name: str = "commontrace", namespace: str = "commontrace"):
        super().__init__(namespace)
        try:
            from falkordb import FalkorDB
        except ImportError:
            raise RuntimeError("Falkor backend requires commontrace[graph]") from None
        self.db = FalkorDB(host=host, port=port, password=password)
        self.graph = self.db.select_graph(graph_name)
        self.graph_name = graph_name

    def _ensure_unique_namespace(self):
        try:
            self._query("CREATE INDEX FOR (s:CommonTraceIndex) ON (s.namespace)", {})
        except Exception as exc:
            if "already indexed" not in str(exc).lower():
                raise
        try:
            self.db.connection.execute_command("GRAPH.CONSTRAINT", "CREATE", self.graph_name,
                "UNIQUE", "NODE", "CommonTraceIndex", "PROPERTIES", 1, "namespace")
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise RuntimeError("Falkor unique-namespace constraint could not be installed") from exc
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            constraints = self._query("CALL db.constraints()", {})
            matching = [r for r in constraints if r[0] == "UNIQUE" and r[1] == "CommonTraceIndex"
                        and r[2] in ("[namespace]", ["namespace"]) and r[3] == "NODE"]
            if matching and matching[0][4] == "OPERATIONAL":
                return
            if matching and matching[0][4] == "FAILED":
                raise RuntimeError("Falkor namespace uniqueness failed")
            time.sleep(.02)
        raise TimeoutError("Falkor namespace uniqueness did not become operational")

    def _query(self, statement, params):
        return self.graph.query(statement, params=params).result_set

    def close(self):
        self.db.close()


def rebuild(root: str, backend) -> dict:
    """Publish a complete canonical snapshot; failed writes retain previous visibility."""
    from commontrace import graph

    nodes = graph.load_nodes(root)
    edges = graph.load_edges(root)
    backend.begin_snapshot(hashlib.sha256(os.path.realpath(root).encode()).hexdigest())
    for node in nodes.values():
        if not node.is_forgotten:
            backend.write_node(node.id, node.to_dict())
    count = 0
    for edge in edges:
        if edge.source in nodes and edge.target in nodes and not (
                nodes[edge.source].is_forgotten or nodes[edge.target].is_forgotten):
            backend.write_edge(edge.source, edge.target, relation=edge.relation, weight=edge.weight,
                               valid_at=edge.valid_at, invalid_at=edge.invalid_at, expired_at=edge.expired_at,
                               created_at=edge.created_at or None,
                               closed_recorded_at=(edge.properties or {}).get("closed_recorded_at"),
                               properties=edge.properties)
            count += 1
    backend.publish_snapshot()
    return {"nodes": sum(not n.is_forgotten for n in nodes.values()), "edges": count,
            "canonical_source": "local files", "index_only": True}
