"""Optional source-bound graph snapshots; local canonical evidence remains authoritative."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone


def _moment(value=None):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).isoformat()


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

    def write_edge(self, source: str, target: str, *, weight: float = 1., valid_from=None, valid_until=None):
        if not self.generation:
            raise ValueError("begin a source-bound graph snapshot before writing")
        self._query("MATCH (a:CommonTrace {id:$source,namespace:$ns,generation:$gen}), "
                    "(b:CommonTrace {id:$target,namespace:$ns,generation:$gen}) "
                    "CREATE (a)-[e:MEMORY]->(b) SET e.weight=$weight,e.valid_from=$start,e.valid_until=$end",
                    {"source": source, "target": target, "ns": self.namespace, "gen": self.generation,
                     "weight": weight, "start": _moment(valid_from) if valid_from else None,
                     "end": _moment(valid_until) if valid_until else None})

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

    def neighbors(self, node_id: str, *, as_of=None) -> list[dict]:
        result = self._query("MATCH (s:CommonTraceIndex {namespace:$ns}), "
            "(n:CommonTrace {id:$id,namespace:$ns})-[e:MEMORY]-(m:CommonTrace {namespace:$ns}) "
            "WHERE n.generation=s.current AND m.generation=s.current AND "
            "(e.valid_from IS NULL OR e.valid_from <= $at) AND (e.valid_until IS NULL OR e.valid_until > $at) "
            "RETURN m.id,coalesce(e.weight,1.0) LIMIT 2000",
            {"ns": self.namespace, "id": node_id, "at": _moment(as_of)})
        return [{"neighbor_id": row[0], "weight": row[1]} for row in result]


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
            backend.write_edge(edge.source, edge.target, weight=edge.weight,
                               valid_from=edge.valid_at, valid_until=edge.invalid_at or edge.expired_at)
            count += 1
    backend.publish_snapshot()
    return {"nodes": sum(not n.is_forgotten for n in nodes.values()), "edges": count,
            "canonical_source": "local files", "index_only": True}
