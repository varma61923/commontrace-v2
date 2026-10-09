"""A store's declarative ontology: which entity types and relations its knowledge graph
uses, what each relation connects, which relations hold one value at a time, and
which names are aliases of one entity.

It lives in `memory/ontology.yaml` (or `.json`), or an RDF/OWL file
(`memory/ontology.ttl`, `.owl`, `.rdf`) when rdflib is installed. Without one the
built-in defaults apply. The graph reads it on every write: unknown types and
relations fall back (or are refused when `strict: true`), aliases resolve to one
node, and an exclusive relation closes its previous value when a newer one is
asserted."""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field

from commontrace import paths

FILENAMES = ("ontology.yaml", "ontology.yml", "ontology.json", "ontology.ttl", "ontology.owl", "ontology.rdf")

DEFAULT_ENTITY_TYPES = (
    "service", "tool", "error", "concept", "lesson", "scope", "user", "file", "symbol", "memory", "document",
    "person", "place", "organization", "event",
)
DEFAULT_RELATIONS: dict[str, dict] = {
    "depends_on": {"inverse": "required_by"},
    "causes": {},
    "resolves": {},
    "affects": {},
    "supersedes": {},
    "scoped_to": {},
    "uses": {},
    "violates": {},
    "relates_to": {"symmetric": True},
    "contains": {"inverse": "part_of"},
    "mentions": {},
    "raises": {},
    "updates": {},
    "extends": {},
    "derives": {},
    "owned_by": {"exclusive": True},
    "located_in": {"exclusive": True},
    "status": {"exclusive": True},
    "works_at": {"domain": ["person", "user"], "range": ["organization"], "exclusive": True},
}
FALLBACK_TYPE = "concept"
FALLBACK_RELATION = "relates_to"


class OntologyError(ValueError):
    """An ontology file that cannot be read, or a write it forbids."""


@dataclass(frozen=True)
class EntityType:
    name: str
    description: str = ""
    parent: str | None = None
    schema: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RelationType:
    name: str
    description: str = ""
    domain: tuple[str, ...] = ()
    range: tuple[str, ...] = ()
    exclusive: bool = False
    symmetric: bool = False
    inverse: str | None = None


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


@dataclass
class Ontology:
    entity_types: dict[str, EntityType] = field(default_factory=dict)
    relations: dict[str, RelationType] = field(default_factory=dict)
    entity_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    strict: bool = False
    source: str = "built-in"

    @classmethod
    def from_json_schema(cls, schema: dict) -> Ontology:
        """Import prescribed entity models without making Pydantic mandatory."""
        definitions = schema.get("$defs", schema.get("definitions", {}))
        if not definitions and schema.get("title"):
            definitions = {schema["title"]: schema}
        if not isinstance(definitions, dict) or not definitions:
            raise OntologyError("ontology schema needs named object definitions")
        base = cls.default()
        base.source = "json-schema"
        for name, spec in definitions.items():
            if isinstance(spec, dict) and spec.get("type") == "object":
                key = _key(name)
                base.entity_types[key] = EntityType(key, str(spec.get("description", "")),
                                                   schema={**spec, "$defs": definitions})
        if schema.get("x-commontrace-relations"):
            base = _from_mapping({"entity_types": base.to_dict()["entity_types"],
                                  "relations": schema["x-commontrace-relations"]}, "json-schema")
        return base

    @classmethod
    def from_models(cls, models: list) -> Ontology:
        """Accept Pydantic v2/v1 models through their public JSON-schema API."""
        definitions = {}
        for model in models:
            factory = getattr(model, "model_json_schema", None) or getattr(model, "schema", None)
            if factory is None:
                raise OntologyError("model does not expose a JSON schema")
            schema = factory()
            definitions[schema.get("title", model.__name__)] = schema
            definitions.update(schema.get("$defs", schema.get("definitions", {})))
        return cls.from_json_schema({"$defs": definitions})

    @classmethod
    def default(cls) -> Ontology:
        return cls(
            entity_types={n: EntityType(n) for n in DEFAULT_ENTITY_TYPES},
            relations={n: RelationType(n, domain=tuple(o.get("domain", ())), range=tuple(o.get("range", ())),
                                       exclusive=o.get("exclusive", False), symmetric=o.get("symmetric", False),
                                       inverse=o.get("inverse"))
                       for n, o in DEFAULT_RELATIONS.items()},
        )

    # --- lookups ------------------------------------------------------------------

    def ancestors(self, type_name: str) -> list[str]:
        out, seen = [], set()
        current = self.entity_types.get(type_name)
        while current is not None and current.name not in seen:
            seen.add(current.name)
            out.append(current.name)
            current = self.entity_types.get(current.parent) if current.parent else None
        return out

    def entity_type(self, name: str) -> str:
        key = _key(name)
        if key in self.entity_types:
            return key
        if self.strict:
            raise OntologyError(f"entity type {name!r} is not in the ontology ({self.source})")
        return FALLBACK_TYPE

    def relation(self, name: str) -> RelationType:
        key = _key(name)
        if key in self.relations:
            return self.relations[key]
        for rel in self.relations.values():
            if rel.inverse and _key(rel.inverse) == key:
                return rel
        if self.strict:
            raise OntologyError(f"relation {name!r} is not in the ontology ({self.source})")
        return self.relations.get(FALLBACK_RELATION) or RelationType(FALLBACK_RELATION, symmetric=True)

    def is_inverse(self, name: str) -> bool:
        key = _key(name)
        return key not in self.relations and any(r.inverse and _key(r.inverse) == key for r in self.relations.values())

    def check_edge(self, relation: RelationType, source_type: str, target_type: str) -> list[str]:
        problems = []
        if relation.domain and not set(self.ancestors(source_type)) & set(relation.domain):
            problems.append(f"{relation.name} expects a source of type {'/'.join(relation.domain)}, "
                            f"not {source_type}")
        if relation.range and not set(self.ancestors(target_type)) & set(relation.range):
            problems.append(f"{relation.name} expects a target of type {'/'.join(relation.range)}, "
                            f"not {target_type}")
        if problems and self.strict:
            raise OntologyError("; ".join(problems))
        return problems

    def canonical_id(self, node_id: str) -> str:
        """The node an alias names: `service:pg` -> `service:postgres` when declared."""
        lowered = node_id.strip().lower()
        for canonical, aliases in self.entity_aliases.items():
            if lowered == canonical.lower():
                return canonical
            prefix = canonical.split(":", 1)[0] + ":" if ":" in canonical else ""
            for alias in aliases:
                alias_l = alias.lower()
                if lowered == alias_l or (prefix and lowered == prefix + alias_l) or \
                        (prefix and lowered.split(":", 1)[-1] == alias_l.replace(" ", "_")):
                    return canonical
        return node_id

    def aliases(self) -> dict[str, list[str]]:
        """{canonical name: variants} for text canonicalisation at ingestion."""
        out = {}
        for canonical, variants in self.entity_aliases.items():
            name = canonical.split(":", 1)[-1].replace("_", " ")
            out[name] = [v for v in variants if v.lower() != name.lower()]
        return {k: v for k, v in out.items() if v}

    def to_dict(self) -> dict:
        return {
            "source": self.source, "strict": self.strict,
            "entity_types": {n: {"description": t.description, "parent": t.parent, "schema": t.schema}
                             for n, t in sorted(self.entity_types.items())},
            "relations": {n: {"description": r.description, "domain": list(r.domain), "range": list(r.range),
                              "exclusive": r.exclusive, "symmetric": r.symmetric, "inverse": r.inverse}
                          for n, r in sorted(self.relations.items())},
            "aliases": {k: list(v) for k, v in sorted(self.entity_aliases.items())},
        }


# --- loading ------------------------------------------------------------------------

def _from_mapping(data: dict, source: str) -> Ontology:
    if not isinstance(data, dict):
        raise OntologyError(f"{source}: the ontology must be a mapping")
    base = Ontology.default() if data.get("extends_default", True) else Ontology()
    base.source, base.strict = source, bool(data.get("strict", False))
    for name, spec in (data.get("entity_types") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        base.entity_types[_key(name)] = EntityType(_key(name), str(spec.get("description", "")),
                                                   _key(spec["parent"]) if spec.get("parent") else None,
                                                   spec.get("schema", {}))
    for name, spec in (data.get("relations") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        as_tuple = lambda v: tuple(_key(x) for x in ([v] if isinstance(v, str) else (v or ())))  # noqa: E731
        base.relations[_key(name)] = RelationType(
            _key(name), str(spec.get("description", "")), as_tuple(spec.get("domain")), as_tuple(spec.get("range")),
            bool(spec.get("exclusive", False)), bool(spec.get("symmetric", False)),
            _key(spec["inverse"]) if spec.get("inverse") else None)
    for canonical, variants in (data.get("aliases") or {}).items():
        if not isinstance(variants, (list, tuple)):
            raise OntologyError(f"{source}: aliases of {canonical!r} must be a list")
        base.entity_aliases[str(canonical).strip().lower()] = tuple(str(v).strip() for v in variants if str(v).strip())
    for rel in base.relations.values():
        for t in (*rel.domain, *rel.range):
            if t not in base.entity_types:
                raise OntologyError(f"{source}: relation {rel.name} names unknown entity type {t!r}")
    return base


def _from_rdf(path: str) -> Ontology:
    try:
        import rdflib
        from rdflib.namespace import OWL, RDF, RDFS, SKOS
    except ImportError:
        raise OntologyError(f"{path}: reading RDF/OWL needs rdflib (pip install rdflib)") from None
    g = rdflib.Graph()
    try:
        g.parse(path)
    except Exception as exc:  # noqa: BLE001 - any parser failure is a bad file
        raise OntologyError(f"{path}: {exc}") from None

    def local(term) -> str:
        text = str(term)
        return _key(re.split(r"[#/]", text)[-1])

    data: dict = {"entity_types": {}, "relations": {}, "aliases": {}}
    for cls in set(g.subjects(RDF.type, OWL.Class)) | set(g.subjects(RDF.type, RDFS.Class)):
        if isinstance(cls, rdflib.BNode):
            continue
        parent = next((local(p) for p in g.objects(cls, RDFS.subClassOf) if not isinstance(p, rdflib.BNode)), None)
        data["entity_types"][local(cls)] = {"parent": parent,
                                            "description": str(next(g.objects(cls, RDFS.comment), ""))}
    for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        data["relations"][local(prop)] = {
            "domain": [local(d) for d in g.objects(prop, RDFS.domain)],
            "range": [local(r) for r in g.objects(prop, RDFS.range)],
            "exclusive": (prop, RDF.type, OWL.FunctionalProperty) in g,
            "symmetric": (prop, RDF.type, OWL.SymmetricProperty) in g,
            "inverse": next((local(i) for i in g.objects(prop, OWL.inverseOf)), None),
            "description": str(next(g.objects(prop, RDFS.comment), "")),
        }
    for subject, alt in g.subject_objects(SKOS.altLabel):
        types = [local(t) for t in g.objects(subject, RDF.type) if local(t) in data["entity_types"]]
        canonical = f"{types[0]}:{local(subject)}" if types else local(subject)
        data["aliases"].setdefault(canonical, []).append(str(alt))
    for rel in data["relations"].values():
        for t in (*rel["domain"], *rel["range"]):
            data["entity_types"].setdefault(t, {})
    return _from_mapping(data, path)


def ontology_path(root: str) -> str | None:
    for name in FILENAMES:
        path = os.path.join(paths.memory_dir(root), name)
        if os.path.isfile(path):
            return path
    return None


_CACHE: dict[str, tuple[tuple, Ontology]] = {}
_LOCK = threading.Lock()


def load(root: str) -> Ontology:
    """The store's ontology (cached until the file changes), or the defaults."""
    path = ontology_path(root)
    if path is None:
        return Ontology.default()
    st = os.stat(path)
    stamp = (path, st.st_mtime_ns, st.st_size)
    with _LOCK:
        cached = _CACHE.get(root)
        if cached and cached[0] == stamp:
            return cached[1]
    if path.endswith((".ttl", ".owl", ".rdf")):
        onto = _from_rdf(path)
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if path.endswith(".json"):
            data = json.loads(text)
        else:
            import yaml

            data = yaml.safe_load(text) or {}
        onto = _from_mapping(data, path)
    with _LOCK:
        _CACHE[root] = (stamp, onto)
    return onto


TEMPLATE = """# CommonTrace ontology: the types and relations this store's knowledge graph uses.
# Built-in types and relations stay available unless extends_default is false.
strict: false            # true: refuse unknown types/relations and domain/range violations
extends_default: true

entity_types:
  database: {parent: service, description: A data store}
  team: {parent: organization}

relations:
  hosted_on: {domain: [service], range: [service], exclusive: true,
              description: Where a service runs; one host at a time}
  owns: {domain: [team, person], range: [service], inverse: owned_by}

aliases:
  service:postgres: [pg, postgresql, postgres db]
"""
