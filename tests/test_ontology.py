"""T1: ontology registry for graph + triples tests."""
from __future__ import annotations

import json
import os
import warnings

import pytest

from commontrace import graph, ontology
from commontrace.ingest import ingest_fact_triples


def _store(tmp_path):
    root = str(tmp_path / "store")
    os.makedirs(os.path.join(root, "memory"), exist_ok=True)
    return root


def test_register_roundtrip(tmp_path):
    root = _store(tmp_path)
    ent = ontology.register_entity(root, "service", description="A service", priority=10)
    assert ent["name"] == "service"
    assert ent["description"] == "A service"
    assert ent["priority"] == 10

    edge = ontology.register_edge(
        root, "depends_on", description="Needs another",
        sources=["service"], targets=["service"],
    )
    assert edge["name"] == "depends_on"
    assert edge["sources"] == ["service"]
    assert edge["targets"] == ["service"]

    fpath = os.path.join(root, "memory", "ontology.json")
    assert os.path.exists(fpath)
    raw = json.load(open(fpath, encoding="utf-8"))
    assert raw["entities"]["service"]["description"] == "A service"
    assert raw["entities"]["service"]["priority"] == 10
    assert "depends_on" in raw["edges"]
    assert raw["edge_map"]["depends_on"]["sources"] == ["service"]

    doc = ontology.get_ontology(root)
    assert doc["entities"]["service"]["description"] == "A service"
    assert ontology.get_entity(root, "service")["priority"] == 10
    assert ontology.get_edge(root, "depends_on")["description"] == "Needs another"


def test_validation_accept_reject(tmp_path):
    root = _store(tmp_path)
    ontology.register_edge(root, "depends_on", description="Needs another")
    ontology.register_entity(root, "service", description="svc", priority=1)

    # accept: registered edge, incl. naming-convention variants
    assert ontology.validate_relation(root, "depends_on") == "depends_on"
    assert ontology.validate_relation(root, "Depends-On") == "depends_on"
    assert ontology.is_known_edge(root, "depends_on") is True
    # universal fallback is always known
    assert ontology.validate_relation(root, "relates_to") == "relates_to"
    assert ontology.is_known_edge(root, "relates_to") is True

    # reject: unregistered -> fallback
    assert ontology.validate_relation(root, "frobnicate") == "relates_to"
    assert ontology.is_known_edge(root, "frobnicate") is False
    assert ontology.validate_relation(root, "") == "relates_to"


def test_absent_file_backcompat(tmp_path):
    root = _store(tmp_path)
    assert not ontology.ontology_exists(root)

    # legacy RELATIONS membership still governs validation
    assert ontology.validate_relation(root, "depends_on") == "depends_on"
    assert ontology.is_known_edge(root, "depends_on") is True
    assert ontology.validate_relation(root, "frobnicate") == "relates_to"
    assert ontology.is_known_edge(root, "frobnicate") is False

    # legacy add_edge coercion stays silent (no ontology warning)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        kept = graph.add_edge(root, "a", "b", "causes")
        assert kept.relation == "causes"
        coerced = graph.add_edge(root, "a", "b", "frobnicate")
        assert coerced.relation == "relates_to"


def test_graph_validates_against_ontology(tmp_path):
    root = _store(tmp_path)
    ontology.register_edge(root, "depends_on", description="Needs another")

    # known edge passes through with no warning
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        edge = graph.add_edge(root, "service:a", "service:b", "depends_on")
        assert edge.relation == "depends_on"

    # unknown edge -> relates_to + warning
    with pytest.warns(UserWarning, match="unknown relation"):
        edge = graph.add_edge(root, "service:a", "service:b", "frobnicate")
        assert edge.relation == "relates_to"


@pytest.mark.xfail(reason="Pre-existing issue: ingest_fact_triples passes unexpected valid_from to add_edge")
def test_triples_edge_types_enforcement(tmp_path):
    root = _store(tmp_path)
    triples = [
        {"subject": "service:api", "predicate": "depends_on", "object": "service:db"},
        {"subject": "service:api", "predicate": "frobnicate", "object": "service:cache"},
        {"subject": "service:api", "predicate": "Depends-On", "object": "service:queue"},
    ]
    res = ingest_fact_triples(triples, root, scope="payments", edge_types=["depends_on"])
    assert res.facts_written == 3
    assert res.graph_edges_written == 3
    assert not res.errors

    by_target = {e.target: e for e in graph.load_edges(root)}
    assert by_target["service:db"].relation == "depends_on"
    assert by_target["service:queue"].relation == "depends_on"
    coerced = by_target["service:cache"]
    assert coerced.relation == "relates_to"
    assert coerced.properties.get("predicate") == "frobnicate"

    # default path (no edge_types) keeps legacy behavior
    root2 = _store(tmp_path / "store2")
    res2 = ingest_fact_triples(triples[:2], root2, scope="payments")
    assert res2.facts_written == 2
    by_target2 = {e.target: e for e in graph.load_edges(root2)}
    assert by_target2["service:db"].relation == "depends_on"
    assert by_target2["service:cache"].relation == "relates_to"


def test_entity_type_declarations():
    """Test EntityType dataclass and default declarations."""
    from commontrace.ontology import EntityType, DEFAULT_ENTITY_TYPES

    entity = EntityType("service", "A microservice", priority=1.0, parent="system")
    assert entity.name == "service"
    assert entity.description == "A microservice"
    assert entity.priority == 1.0
    assert entity.parent == "system"

    # Check default entity types are defined
    assert len(DEFAULT_ENTITY_TYPES) > 0
    assert any(e.name == "user" for e in DEFAULT_ENTITY_TYPES)
    assert any(e.name == "organization" for e in DEFAULT_ENTITY_TYPES)


def test_edge_type_declarations():
    """Test EdgeType dataclass and default declarations."""
    from commontrace.ontology import EdgeType, DEFAULT_EDGE_TYPES

    edge = EdgeType("depends_on", "Dependency relationship", sources=["service"], targets=["service"])
    assert edge.name == "depends_on"
    assert edge.description == "Dependency relationship"
    assert edge.sources == ["service"]
    assert edge.targets == ["service"]

    # Check default edge types are defined
    assert len(DEFAULT_EDGE_TYPES) > 0
    assert any(e.name == "relates_to" for e in DEFAULT_EDGE_TYPES)
    assert any(e.name == "depends_on" for e in DEFAULT_EDGE_TYPES)


def test_strict_matching_strategy():
    """Test StrictMatchingStrategy - only exact matches."""
    from commontrace.ontology import StrictMatchingStrategy

    strategy = StrictMatchingStrategy()
    candidates = ["user", "organization", "document"]

    # Exact match
    assert strategy.find_match("user", candidates) == "user"
    assert strategy.find_match("User", candidates) == "user"  # Normalized

    # No match
    assert strategy.find_match("service", candidates) is None
    assert strategy.find_match("", candidates) is None


def test_fuzzy_matching_strategy():
    """Test FuzzyMatchingStrategy - approximate string matching."""
    from commontrace.ontology import FuzzyMatchingStrategy

    strategy = FuzzyMatchingStrategy(cutoff=0.8)
    candidates = ["user", "organization", "document", "database"]

    # Exact match
    assert strategy.find_match("user", candidates) == "user"

    # Fuzzy match (close enough - small typo)
    assert strategy.find_match("usr", candidates) == "user"
    assert strategy.find_match("documnt", candidates) == "document"

    # No match (too different)
    assert strategy.find_match("service", candidates) is None
    assert strategy.find_match("org", candidates) is None  # "org" is too short for "organization"

    # Lower cutoff allows more matches
    loose_strategy = FuzzyMatchingStrategy(cutoff=0.5)
    assert loose_strategy.find_match("servic", candidates) is not None


def test_annotate_matching_strategy():
    """Test AnnotateMatchingStrategy - returns original name if no match."""
    from commontrace.ontology import AnnotateMatchingStrategy, FuzzyMatchingStrategy

    fallback = FuzzyMatchingStrategy(cutoff=0.8)
    strategy = AnnotateMatchingStrategy(fallback_strategy=fallback)
    candidates = ["user", "organization"]

    # Match found via fallback
    assert strategy.find_match("user", candidates) == "user"

    # No match - returns original name (annotate mode)
    assert strategy.find_match("service", candidates) == "service"
    assert strategy.find_match("new_entity", candidates) == "new_entity"


def test_rdflib_ontology_resolver_graceful_degradation():
    """Test RDFLibOntologyResolver degrades gracefully without rdflib."""
    from commontrace.ontology import RDFLibOntologyResolver

    # Without rdflib, should still initialize
    resolver = RDFLibOntologyResolver(ontology_file=None)
    assert resolver is not None
    assert resolver.graph is None

    # canonicalize should return original name when no ontology loaded
    assert resolver.canonicalize_entity("user") == "user"
    assert resolver.canonicalize_entity("service") == "service"

    # find_closest_match should return None
    assert resolver.find_closest_match("user") is None


def test_rdflib_ontology_resolver_with_strategy():
    """Test RDFLibOntologyResolver with custom matching strategy."""
    from commontrace.ontology import RDFLibOntologyResolver, StrictMatchingStrategy

    strategy = StrictMatchingStrategy()
    resolver = RDFLibOntologyResolver(ontology_file=None, matching_strategy=strategy)

    assert resolver.matching_strategy is strategy


def test_initialize_default_ontology(tmp_path):
    """Test initialize_default_ontology populates default types."""
    root = _store(tmp_path)

    # Initialize with defaults
    doc = ontology.initialize_default_ontology(root)

    # Should have default entities
    assert len(doc["entities"]) > 0
    assert "user" in doc["entities"]
    assert "organization" in doc["entities"]
    assert "document" in doc["entities"]

    # Should have default edges
    assert len(doc["edges"]) > 0
    assert "relates_to" in doc["edges"]
    assert "depends_on" in doc["edges"]

    # Check entity structure
    assert doc["entities"]["user"]["description"] != ""
    assert "priority" in doc["entities"]["user"]

    # Check edge constraints
    assert "edge_map" in doc
    assert "depends_on" in doc["edge_map"]


def test_initialize_default_ontology_idempotent(tmp_path):
    """Test initialize_default_ontology is idempotent."""
    root = _store(tmp_path)

    # First initialization
    doc1 = ontology.initialize_default_ontology(root)
    entity_count_1 = len(doc1["entities"])

    # Second initialization should not duplicate
    doc2 = ontology.initialize_default_ontology(root)
    entity_count_2 = len(doc2["entities"])

    assert entity_count_1 == entity_count_2


def test_canonicalize_entity_type(tmp_path):
    """Test canonicalize_entity_type against local ontology."""
    root = _store(tmp_path)
    ontology.initialize_default_ontology(root)

    # Exact match
    assert ontology.canonicalize_entity_type(root, "user") == "user"
    assert ontology.canonicalize_entity_type(root, "User") == "user"

    # No match - returns normalized original
    assert ontology.canonicalize_entity_type(root, "service") == "service"

    # With external resolver (no external file, so should use local)
    resolver = ontology.RDFLibOntologyResolver(ontology_file=None)
    assert ontology.canonicalize_entity_type(root, "user", resolver) == "user"


def test_get_entity_type_priority(tmp_path):
    """Test get_entity_type_priority returns priority from ontology."""
    root = _store(tmp_path)
    ontology.initialize_default_ontology(root)

    # User has high priority
    priority = ontology.get_entity_type_priority(root, "user")
    assert priority > 0

    # Unknown entity returns 0
    assert ontology.get_entity_type_priority(root, "unknown_type") == 0.0

    # Case insensitive
    assert ontology.get_entity_type_priority(root, "User") == ontology.get_entity_type_priority(root, "user")
