"""Prescribed schemas and evidence-linked learned ontology proposals."""
from __future__ import annotations

from collections import Counter

from commontrace import _jsonl, graph, memory_control, ontology, paths


def propose(root: str) -> dict:
    nodes, edges = graph.load_nodes(root), graph.load_edges(root)
    counts = Counter(node.entity_type for node in nodes.values())
    definitions = {name: {"type": "object", "description": f"Observed on {count} graph nodes"}
                   for name, count in counts.items()}
    property_types = {}
    for node in nodes.values():
        types = property_types.setdefault(node.entity_type, {})
        for key, value in node.properties.items():
            kind = ("null" if value is None else "boolean" if isinstance(value, bool)
                    else "integer" if isinstance(value, int) else "number" if isinstance(value, float)
                    else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object")
            types.setdefault(key, set()).add(kind)
    for name, properties in property_types.items():
        definitions[name]["properties"] = {key: {"type": sorted(types)} for key, types in properties.items()}
    relations = {}
    for edge in edges:
        spec = relations.setdefault(edge.relation, {"domain": set(), "range": set()})
        if edge.source in nodes:
            spec["domain"].add(nodes[edge.source].entity_type)
        if edge.target in nodes:
            spec["range"].add(nodes[edge.target].entity_type)
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": definitions,
              "x-commontrace-relations": {name: {k: sorted(v) for k, v in spec.items()}
                                           for name, spec in relations.items()}}
    if not definitions:
        raise ValueError("no graph evidence available to learn an ontology")
    return memory_control.put(root, "proposal", "Learned ontology from observed graph structure",
                               data={"status": "review", "schema": schema, "sources": sorted(nodes)})


def prescribe(root: str, schema: dict, *, replace: bool = False) -> dict:
    """Explicit operator command; never called automatically by the learner."""
    onto = ontology.Ontology.from_json_schema(schema)
    existing = ontology.ontology_path(root)
    if existing and not replace:
        raise ValueError("ontology exists; explicit replacement is required")
    if existing and not existing.endswith(".json"):
        raise ValueError("remove or migrate the existing non-JSON ontology before replacing it")
    import os

    filename = os.path.join(paths.memory_dir(root), "ontology.json")
    _jsonl.write_json(filename, onto.to_dict())
    return onto.to_dict()
