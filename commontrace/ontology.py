"""Ontology registry for graph edge types and entity types.

Persists a per-store registry at ``memory/ontology.json`` with three sections:

- ``entities``: ``{name: {"description": str, "priority": number}}``
- ``edges``: ``{name: {"description": str}}``
- ``edge_map``: ``{edge_name: {"sources": [...], "targets": [...]}}``
  advisory source/target entity-type constraints per edge (empty lists mean
  unconstrained).

When the registry file is absent, :mod:`commontrace.graph` falls back to its
legacy ``RELATIONS`` membership check (back-compatible). When present, unknown
edge types are coerced to ``"relates_to"`` with a warning.

Enhanced with EntityType/EdgeType declarations, RDFLib-based ontology resolvers
with matching strategies (annotate vs strict mode), and entity canonicalization.
"""
from __future__ import annotations

import difflib
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from commontrace import paths

ONTOLOGY_FILENAME = "ontology.json"
FALLBACK_RELATION = "relates_to"

_EDGE_NORM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# EntityType and EdgeType declarations
# ---------------------------------------------------------------------------


@dataclass
class EntityType:
    """Declaration of an entity type in the ontology."""
    name: str
    description: str = ""
    priority: float = 0.0
    # Optional parent type for hierarchy
    parent: str | None = None


@dataclass
class EdgeType:
    """Declaration of an edge/relation type in the ontology."""
    name: str
    description: str = ""
    # Advisory source/target entity-type constraints
    sources: list[str] | None = None
    targets: list[str] | None = None


# Default ontology with common types (migrated from hard-coded types)
DEFAULT_ENTITY_TYPES = [
    EntityType("user", "A user or person in the system", priority=1.0),
    EntityType("assistant", "AI assistant or agent", priority=1.0),
    EntityType("organization", "Company, institution, or group", priority=0.8),
    EntityType("document", "Information content (article, report, email)", priority=0.7),
    EntityType("event", "Time-bound activity or occurrence", priority=0.7),
    EntityType("location", "Physical or virtual place", priority=0.6),
    EntityType("preference", "User preference or choice", priority=0.9),
    EntityType("topic", "Subject of conversation or knowledge domain", priority=0.5),
    EntityType("object", "Physical item, tool, or device", priority=0.4),
    EntityType("system", "Software system or component", priority=0.6),
    EntityType("database", "Database or data store", priority=0.6),
    EntityType("service", "API or microservice", priority=0.6),
]

DEFAULT_EDGE_TYPES = [
    EdgeType("relates_to", "Generic relationship"),
    EdgeType("located_at", "Entity exists at a location", sources=["user", "event", "organization"], targets=["location"]),
    EdgeType("occurred_at", "Event happened at a time or location", sources=["event"], targets=["location"]),
    EdgeType("works_for", "Person employed by organization", sources=["user"], targets=["organization"]),
    EdgeType("authored", "Person created document", sources=["user"], targets=["document"]),
    EdgeType("prefers", "User preference for something", sources=["user", "preference"], targets=["topic", "object"]),
    EdgeType("uses", "Entity uses another entity", sources=["user", "system"], targets=["tool", "service", "database"]),
    EdgeType("depends_on", "Entity depends on another", sources=["system", "service"], targets=["system", "service", "database"]),
    EdgeType("part_of", "Entity is part of a larger entity", sources=["system", "service"], targets=["system", "organization"]),
    EdgeType("connected_to", "Generic connection between entities"),
    EdgeType("mentions", "Document mentions entity", sources=["document"], targets=["user", "organization", "topic"]),
]


# ---------------------------------------------------------------------------
# Matching strategies for ontology resolution
# ---------------------------------------------------------------------------


class MatchingStrategy(ABC):
    """Abstract base class for ontology entity matching strategies."""

    @abstractmethod
    def find_match(self, name: str, candidates: list[str]) -> str | None:
        """Find the best match for a given name from a list of candidates.

        Args:
            name: The name to match
            candidates: List of candidate names to match against

        Returns:
            The best matching candidate name, or None if no match found
        """
        pass


class StrictMatchingStrategy(MatchingStrategy):
    """Strict matching: only exact matches are accepted."""

    def find_match(self, name: str, candidates: list[str]) -> str | None:
        """Find exact match only."""
        normalized = normalize_entity_name(name)
        for candidate in candidates:
            if normalize_entity_name(candidate) == normalized:
                return candidate
        return None


class FuzzyMatchingStrategy(MatchingStrategy):
    """Fuzzy matching using difflib for approximate string matching."""

    def __init__(self, cutoff: float = 0.8):
        """Initialize fuzzy matching strategy.

        Args:
            cutoff: Minimum similarity score (0.0 to 1.0) for a match to be considered valid
        """
        self.cutoff = cutoff

    def find_match(self, name: str, candidates: list[str]) -> str | None:
        """Find the closest fuzzy match for a given name."""
        if not candidates:
            return None

        normalized = normalize_entity_name(name)
        normalized_candidates = [normalize_entity_name(c) for c in candidates]

        # Check for exact match first
        if normalized in normalized_candidates:
            return candidates[normalized_candidates.index(normalized)]

        # Find fuzzy match
        best_match = difflib.get_close_matches(normalized, normalized_candidates, n=1, cutoff=self.cutoff)
        if best_match:
            return candidates[normalized_candidates.index(best_match[0])]
        return None


class AnnotateMatchingStrategy(MatchingStrategy):
    """Annotate mode: always returns the original name if no match found.

    This is useful for discovering new entity types without strict validation.
    """

    def __init__(self, fallback_strategy: MatchingStrategy | None = None):
        """Initialize annotate matching strategy.

        Args:
            fallback_strategy: Optional strategy to try before falling back to annotation
        """
        self.fallback_strategy = fallback_strategy or FuzzyMatchingStrategy(cutoff=0.6)

    def find_match(self, name: str, candidates: list[str]) -> str | None:
        """Try fallback strategy first, then return original name if no match."""
        match = self.fallback_strategy.find_match(name, candidates)
        if match:
            return match
        # Annotate mode: return the original name (to be added to ontology later)
        return name


# ---------------------------------------------------------------------------
# RDFLib-based ontology resolver
# ---------------------------------------------------------------------------


class RDFLibOntologyResolver:
    """RDFLib-based ontology resolver for RDF/OWL ontology files.

    Provides entity canonicalization and matching against external ontologies.
    Gracefully degrades if rdflib is not available.
    """

    def __init__(
        self,
        ontology_file: str | None = None,
        matching_strategy: MatchingStrategy | None = None,
    ):
        """Initialize the RDFLib ontology resolver.

        Args:
            ontology_file: Path to RDF/OWL ontology file (Turtle, XML, N-Triples, etc.)
            matching_strategy: Strategy for matching entity names to ontology classes
        """
        self.ontology_file = ontology_file
        self.matching_strategy = matching_strategy or FuzzyMatchingStrategy(cutoff=0.8)
        self.graph = None
        self.lookup: dict[str, dict[str, str]] = {"classes": {}, "individuals": {}}

        if ontology_file:
            self._load_ontology()

    def _load_ontology(self) -> None:
        """Load ontology from file using RDFLib."""
        try:
            from rdflib import Graph
        except ImportError:
            # RDFLib not available - silently degrade
            return

        if not self.ontology_file or not os.path.exists(self.ontology_file):
            return

        try:
            self.graph = Graph()
            self.graph.parse(self.ontology_file)
            self._build_lookup()
        except Exception:
            # Failed to parse - silently degrade
            self.graph = None

    def _build_lookup(self) -> None:
        """Build lookup dictionary from RDF graph."""
        if not self.graph:
            return

        try:
            from rdflib import RDF, RDFS
        except ImportError:
            return

        classes: dict[str, str] = {}
        individuals: dict[str, str] = {}

        # Extract classes (OWL classes or RDFS classes)
        for subj in self.graph.subjects():
            # Simple heuristic: if it has a type, treat as class
            for obj in self.graph.objects(subj, RDF.type):
                key = self._uri_to_key(subj)
                classes[key] = str(subj)

        self.lookup = {"classes": classes, "individuals": individuals}

    def _uri_to_key(self, uri: str) -> str:
        """Extract local name from URI."""
        uri_str = str(uri)
        if "#" in uri_str:
            name = uri_str.split("#")[-1]
        else:
            name = uri_str.rstrip("/").split("/")[-1]
        return name.lower().replace(" ", "_").strip()

    def canonicalize_entity(self, entity_name: str, category: str = "classes") -> str:
        """Canonicalize an entity name against the ontology.

        Args:
            entity_name: The entity name to canonicalize
            category: The category to search ("classes" or "individuals")

        Returns:
            The canonical name from the ontology, or the original if no match
        """
        if not self.lookup.get(category):
            return entity_name

        candidates = list(self.lookup[category].keys())
        normalized = normalize_entity_name(entity_name)
        match = self.matching_strategy.find_match(normalized, candidates)

        if match:
            # Return the original URI form
            return self.lookup[category][get_key_by_value(self.lookup[category], match)]
        return entity_name

    def find_closest_match(self, name: str, category: str = "classes") -> str | None:
        """Find the closest matching entity in the ontology.

        Args:
            name: The name to match
            category: The category to search ("classes" or "individuals")

        Returns:
            The matching entity name, or None if no match found
        """
        if not self.lookup.get(category):
            return None

        candidates = list(self.lookup[category].keys())
        normalized = normalize_entity_name(name)
        return self.matching_strategy.find_match(normalized, candidates)


def get_key_by_value(d: dict, value: str) -> str:
    """Get a dictionary key by its value."""
    for k, v in d.items():
        if v == value:
            return k
    return value


def normalize_edge_name(name: Any) -> str:
    """Normalize an edge/relation name to snake_case lower form."""
    text = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    text = _EDGE_NORM_RE.sub("_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def normalize_entity_name(name: Any) -> str:
    """Normalize an entity-type name (strip + lower)."""
    return str(name or "").strip().lower()


def ontology_file(root: str) -> str:
    """Absolute path of the per-store ontology registry file."""
    return os.path.join(paths.memory_dir(root), ONTOLOGY_FILENAME)


def ontology_exists(root: str) -> bool:
    """Return True when this store has a persisted ontology registry."""
    try:
        return os.path.exists(ontology_file(root))
    except Exception:
        return False


def empty_ontology() -> dict[str, Any]:
    """Return a fresh in-memory ontology document."""
    return {"version": 1, "entities": {}, "edges": {}, "edge_map": {}}


def load_ontology(root: str) -> dict[str, Any]:
    """Load the ontology registry; tolerantly return empty when absent/corrupt."""
    doc = empty_ontology()
    try:
        fpath = ontology_file(root)
    except Exception:
        return doc
    if not os.path.exists(fpath):
        return doc
    try:
        with open(fpath, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return doc
    if not isinstance(raw, dict):
        return doc
    for key in ("entities", "edges", "edge_map"):
        section = raw.get(key)
        if isinstance(section, dict):
            doc[key] = section
    if isinstance(raw.get("version"), int):
        doc["version"] = raw["version"]
    return doc


def get_ontology(root: str) -> dict[str, Any]:
    """Return the full ontology document (alias of :func:`load_ontology`)."""
    return load_ontology(root)


def save_ontology(root: str, data: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist the ontology document and return it."""
    doc = empty_ontology()
    if isinstance(data, dict):
        for key in ("entities", "edges", "edge_map"):
            if isinstance(data.get(key), dict):
                doc[key] = data[key]
        if isinstance(data.get("version"), int):
            doc["version"] = data["version"]
    fpath = ontology_file(root)
    parent = os.path.dirname(fpath)
    os.makedirs(parent, exist_ok=True)
    tmp_path = f"{fpath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp_path, fpath)
    return doc


def register_entity(
    root: str,
    name: str,
    description: str = "",
    priority: float = 0,
) -> dict[str, Any]:
    """Register (or update) an entity type; returns the stored record."""
    clean = normalize_entity_name(name)
    if not clean:
        raise ValueError("Entity name cannot be empty")
    try:
        prio: Any = int(priority)
        if isinstance(priority, float) and not float(priority).is_integer():
            prio = float(priority)
    except Exception:
        prio = 0
    doc = load_ontology(root)
    record = {"description": str(description or ""), "priority": prio}
    doc["entities"][clean] = record
    save_ontology(root, doc)
    return {"name": clean, **record}


def register_edge(
    root: str,
    name: str,
    description: str = "",
    sources: list[str] | None = None,
    targets: list[str] | None = None,
) -> dict[str, Any]:
    """Register (or update) an edge type; returns the stored record.

    ``sources``/``targets`` are advisory entity-type constraints recorded in
    ``edge_map``; empty/missing means unconstrained.
    """
    clean = normalize_edge_name(name)
    if not clean:
        raise ValueError("Edge name cannot be empty")
    doc = load_ontology(root)
    doc["edges"][clean] = {"description": str(description or "")}
    src_list = sorted({normalize_entity_name(s) for s in (sources or []) if str(s).strip()})
    dst_list = sorted({normalize_entity_name(t) for t in (targets or []) if str(t).strip()})
    doc["edge_map"][clean] = {"sources": src_list, "targets": dst_list}
    save_ontology(root, doc)
    return {
        "name": clean,
        "description": str(description or ""),
        "sources": src_list,
        "targets": dst_list,
    }


def get_entity(root: str, name: str) -> dict[str, Any] | None:
    """Return the stored entity record (with name) or None."""
    clean = normalize_entity_name(name)
    entities = load_ontology(root).get("entities", {})
    if clean in entities and isinstance(entities[clean], dict):
        return {"name": clean, **entities[clean]}
    return None


def get_edge(root: str, name: str) -> dict[str, Any] | None:
    """Return the stored edge record (with name + endpoint map) or None."""
    clean = normalize_edge_name(name)
    doc = load_ontology(root)
    edges = doc.get("edges", {})
    if clean in edges and isinstance(edges[clean], dict):
        edge_map = doc.get("edge_map", {}).get(clean, {})
        if not isinstance(edge_map, dict):
            edge_map = {}
        return {
            "name": clean,
            **edges[clean],
            "sources": list(edge_map.get("sources", [])),
            "targets": list(edge_map.get("targets", [])),
        }
    return None


def is_known_edge(root: str, relation: Any) -> bool:
    """Return True when ``relation`` is an accepted edge type for this store.

    Falls back to :mod:`commontrace.graph` ``RELATIONS`` when no registry
    file exists (back-compatible). ``"relates_to"`` is always known since it
    is the universal fallback.
    """
    norm = normalize_edge_name(relation)
    if not norm:
        return False
    if norm == FALLBACK_RELATION:
        return True
    if not ontology_exists(root):
        try:
            from commontrace import graph as _graph_mod

            return norm in _graph_mod.RELATIONS or str(relation) in _graph_mod.RELATIONS
        except Exception:
            return False
    edges = load_ontology(root).get("edges", {})
    return norm in edges


def validate_relation(root: str, relation: Any) -> str:
    """Validate an edge type; return the canonical name or ``"relates_to"``.

    Accepts registered (normalized) edge types, rejects everything else to
    the fallback. Pure function — emits no warnings; callers (e.g.
    :func:`commontrace.graph.add_edge`) warn on coercion.
    """
    norm = normalize_edge_name(relation)
    if not norm:
        return FALLBACK_RELATION
    if norm == FALLBACK_RELATION:
        return FALLBACK_RELATION
    if not ontology_exists(root):
        try:
            from commontrace import graph as _graph_mod

            if norm in _graph_mod.RELATIONS or str(relation) in _graph_mod.RELATIONS:
                return norm
        except Exception:
            pass
        return FALLBACK_RELATION
    edges = load_ontology(root).get("edges", {})
    if norm in edges:
        return norm
    return FALLBACK_RELATION


def is_allowed_endpoints(
    root: str,
    relation: Any,
    source_type: str = "",
    target_type: str = "",
) -> bool:
    """Advisory check of ``edge_map`` source/target constraints.

    Returns True when the edge is unconstrained, unregistered, or the given
    endpoint types satisfy the registered constraint lists.
    """
    norm = normalize_edge_name(relation)
    edge_map = load_ontology(root).get("edge_map", {}).get(norm)
    if not isinstance(edge_map, dict):
        return True
    src_ok = True
    dst_ok = True
    sources = edge_map.get("sources") or []
    targets = edge_map.get("targets") or []
    if sources and source_type.strip():
        src_ok = normalize_entity_name(source_type) in set(sources)
    if targets and target_type.strip():
        dst_ok = normalize_entity_name(target_type) in set(targets)
    return bool(src_ok and dst_ok)


# ---------------------------------------------------------------------------
# Default ontology initialization
# ---------------------------------------------------------------------------


def initialize_default_ontology(root: str) -> dict[str, Any]:
    """Initialize the ontology with default entity and edge types.

    Migrates hard-coded types to the ontology registry. This is called
    automatically when no ontology file exists.

    Args:
        root: Memory root directory

    Returns:
        The initialized ontology document
    """
    doc = load_ontology(root)

    # Only add defaults if ontology is empty
    if doc["entities"] and doc["edges"]:
        return doc

    # Add default entity types
    for entity_type in DEFAULT_ENTITY_TYPES:
        name = normalize_entity_name(entity_type.name)
        if name not in doc["entities"]:
            doc["entities"][name] = {
                "description": entity_type.description,
                "priority": entity_type.priority,
            }
            if entity_type.parent:
                doc["entities"][name]["parent"] = normalize_entity_name(entity_type.parent)

    # Add default edge types
    for edge_type in DEFAULT_EDGE_TYPES:
        name = normalize_edge_name(edge_type.name)
        if name not in doc["edges"]:
            doc["edges"][name] = {"description": edge_type.description}
            src_list = sorted({normalize_entity_name(s) for s in (edge_type.sources or []) if s})
            dst_list = sorted({normalize_entity_name(t) for t in (edge_type.targets or []) if t})
            doc["edge_map"][name] = {"sources": src_list, "targets": dst_list}

    save_ontology(root, doc)
    return doc


def canonicalize_entity_type(
    root: str,
    entity_name: str,
    ontology_resolver: RDFLibOntologyResolver | None = None,
) -> str:
    """Canonicalize an entity type name against the ontology.

    First checks the local ontology registry, then optionally uses an
    RDFLib ontology resolver for external ontologies.

    Args:
        root: Memory root directory
        entity_name: The entity name to canonicalize
        ontology_resolver: Optional RDFLib ontology resolver for external ontologies

    Returns:
        The canonical entity type name
    """
    normalized = normalize_entity_name(entity_name)

    # Check local ontology first
    doc = load_ontology(root)
    if normalized in doc["entities"]:
        return normalized

    # Try external ontology resolver if provided
    if ontology_resolver:
        canonical = ontology_resolver.canonicalize_entity(entity_name)
        if canonical != entity_name:
            # Found a match in external ontology - register it locally
            register_entity(root, canonical, description=f"From external ontology: {entity_name}")
            return normalize_entity_name(canonical)

    # Return original if no match found
    return normalized


def get_entity_type_priority(root: str, entity_name: str) -> float:
    """Get the priority of an entity type from the ontology.

    Args:
        root: Memory root directory
        entity_name: The entity type name

    Returns:
        The priority value (0.0 if not found)
    """
    normalized = normalize_entity_name(entity_name)
    doc = load_ontology(root)
    entity = doc["entities"].get(normalized)
    if entity and isinstance(entity, dict):
        try:
            return float(entity.get("priority", 0.0))
        except (TypeError, ValueError):
            pass
    return 0.0
