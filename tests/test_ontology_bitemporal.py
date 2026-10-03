"""Declarative ontology and the bi-temporal knowledge graph: exclusive relations,
inverses, aliases, valid-time and record-time queries, intervals and timelines."""
import json
import os

import pytest

from commontrace import graph, ontology


@pytest.fixture
def root(tmp_path):
    (tmp_path / "memory").mkdir()
    return str(tmp_path)


def _write_ontology(root, text, name="ontology.yaml"):
    with open(os.path.join(root, "memory", name), "w", encoding="utf-8") as fh:
        fh.write(text)


def test_defaults_without_a_file(root):
    onto = ontology.load(root)
    assert onto.source == "built-in"
    assert onto.relation("works_at").exclusive
    assert onto.relation("required_by").name == "depends_on" and onto.is_inverse("required_by")
    assert onto.entity_type("spaceship") == ontology.FALLBACK_TYPE
    assert onto.relation("frobnicates").name == ontology.FALLBACK_RELATION


def test_yaml_ontology_types_relations_aliases_and_strict(root):
    _write_ontology(root, ontology.TEMPLATE)
    onto = ontology.load(root)
    assert onto.ancestors("database") == ["database", "service"]
    assert onto.relation("hosted_on").exclusive
    assert onto.canonical_id("service:pg") == "service:postgres"
    assert onto.canonical_id("service:postgresql") == "service:postgres"
    assert onto.aliases() == {"postgres": ["pg", "postgresql", "postgres db"]}
    assert onto.check_edge(onto.relation("hosted_on"), "database", "service") == []
    assert onto.check_edge(onto.relation("hosted_on"), "person", "service")
    _write_ontology(root, "strict: true\nentity_types: {}\n")
    strict = ontology.load(root)
    with pytest.raises(ontology.OntologyError):
        strict.entity_type("spaceship")
    with pytest.raises(ontology.OntologyError):
        strict.relation("frobnicates")


def test_bad_ontology_is_refused(root):
    _write_ontology(root, "relations:\n  r: {domain: [ghost]}\n")
    with pytest.raises(ontology.OntologyError, match="ghost"):
        ontology.load(root)
    _write_ontology(root, json.dumps({"aliases": {"x": "notalist"}}), "ontology.json")
    os.remove(os.path.join(root, "memory", "ontology.yaml"))
    with pytest.raises(ontology.OntologyError):
        ontology.load(root)


def test_rdf_ontology_when_rdflib_is_installed(root):
    pytest.importorskip("rdflib")
    _write_ontology(root, """
@prefix : <http://example.org/o#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
:Database a owl:Class ; rdfs:subClassOf :Service .
:Service a owl:Class .
:hostedOn a owl:ObjectProperty, owl:FunctionalProperty ; rdfs:domain :Service ; rdfs:range :Service .
:postgres a :Database ; skos:altLabel "pg" .
""", "ontology.ttl")
    onto = ontology.load(root)
    assert onto.relation("hostedon").exclusive
    assert onto.ancestors("database") == ["database", "service"]
    assert onto.canonical_id("database:pg") == "database:postgres"


def test_exclusive_relation_latest_valid_at_wins(root):
    graph.add_edge(root, "person:ana", "organization:acme", "works_at", valid_at="2024-01-01")
    graph.add_edge(root, "person:ana", "organization:globex", "works_at", valid_at="2025-03-01")
    edges = {e.target: e for e in graph.load_edges(root)}
    assert edges["organization:acme"].invalid_at.startswith("2025-03-01")
    assert "superseded by organization:globex" in edges["organization:acme"].properties["closed_reason"]
    assert edges["organization:globex"].invalid_at is None
    now = [n["neighbor_id"] for n in graph.get_neighbors(root, "person:ana", direction="out")]
    then = [n["neighbor_id"] for n in graph.get_neighbors(root, "person:ana", direction="out", as_of="2024-06-01")]
    assert now == ["organization:globex"] and then == ["organization:acme"]


def test_late_arriving_older_fact_stays_history(root):
    graph.add_edge(root, "person:ana", "organization:globex", "works_at", valid_at="2025-03-01")
    graph.add_edge(root, "person:ana", "organization:acme", "works_at", valid_at="2024-01-01")
    edges = {e.target: e for e in graph.load_edges(root)}
    assert edges["organization:globex"].invalid_at is None
    assert edges["organization:acme"].invalid_at.startswith("2025-03-01")


def test_non_exclusive_relations_accumulate(root):
    graph.add_edge(root, "service:api", "service:db", "depends_on", valid_at="2024-01-01")
    graph.add_edge(root, "service:api", "service:cache", "depends_on", valid_at="2025-01-01")
    assert all(e.invalid_at is None for e in graph.load_edges(root))


def test_inverse_relation_and_aliases_resolve(root):
    _write_ontology(root, ontology.TEMPLATE)
    graph.add_edge(root, "service:db", "service:api", "required_by")
    graph.add_edge(root, "service:api", "service:pg", "depends_on")
    pairs = {(e.source, e.target, e.relation) for e in graph.load_edges(root)}
    assert ("service:api", "service:db", "depends_on") in pairs
    assert ("service:api", "service:postgres", "depends_on") in pairs


def test_domain_range_warnings(root):
    edge = graph.add_edge(root, "file:a.py", "file:b.py", "works_at")
    assert edge.properties["ontology_warnings"]


def test_known_at_reads_what_the_store_knew(root, monkeypatch):
    monkeypatch.setattr(graph, "_now", lambda: "2025-01-01T00:00:00+00:00")
    graph.add_edge(root, "person:ana", "organization:acme", "works_at", valid_at="2024-01-01")
    monkeypatch.setattr(graph, "_now", lambda: "2025-06-01T00:00:00+00:00")
    graph.add_edge(root, "person:ana", "organization:globex", "works_at", valid_at="2025-03-01")
    believed = graph.get_neighbors(root, "person:ana", direction="out", as_of="2025-04-01",
                                   known_at="2025-02-01")
    assert [n["neighbor_id"] for n in believed] == ["organization:acme"]
    actual = graph.get_neighbors(root, "person:ana", direction="out", as_of="2025-04-01")
    assert [n["neighbor_id"] for n in actual] == ["organization:globex"]


def test_interval_query_and_timeline(root):
    graph.add_edge(root, "person:ana", "organization:acme", "works_at", valid_at="2022-01-01")
    graph.add_edge(root, "person:ana", "organization:globex", "works_at", valid_at="2024-01-01")
    graph.add_edge(root, "person:ana", "place:paris", "located_in", valid_at="2023-05-01")
    in_2022 = graph.edges_between(root, "2022-02-01", "2022-12-31", entity="person:ana")
    assert [e.target for e in in_2022] == ["organization:acme"]
    spanning = graph.edges_between(root, "2023-01-01", "2024-06-01", relation="works_at")
    assert [e.target for e in spanning] == ["organization:acme", "organization:globex"]
    with pytest.raises(ValueError):
        graph.edges_between(root, "2025-01-01", "2024-01-01")
    events = graph.timeline(root, "person:ana")
    assert [(e["event"], e["target"]) for e in events] == [
        ("began", "organization:acme"), ("began", "place:paris"),
        ("ended", "organization:acme"), ("began", "organization:globex"),
    ]
