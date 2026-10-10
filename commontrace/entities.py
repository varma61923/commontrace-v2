"""Entity extraction and linking: find the people, organisations, places, services,
tools, errors, files and symbols a text mentions, resolve each to one canonical graph
node (ontology aliases and surface variants collapse together), and link lessons to
the entities they talk about so retrieval can boost lessons near a query's entities.

spaCy is used when it and a model are installed (`COMMONTRACE_SPACY_MODEL`, default
`en_core_web_sm`); otherwise deterministic patterns do the work. Both paths produce
the same `Mention` records, and the patterns always run, because the technical
entities that matter most to coding agents (error classes, files, env vars,
services) are ones general NER models miss."""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass

from commontrace import paths
from commontrace.ontology import FALLBACK_TYPE

SPACY_LABELS = {
    "PERSON": "person", "ORG": "organization", "GPE": "place", "LOC": "place", "FAC": "place",
    "EVENT": "event", "PRODUCT": "tool", "WORK_OF_ART": "concept", "LAW": "concept", "NORP": "concept",
}

KNOWN_SERVICES = (
    "postgres", "postgresql", "mysql", "mariadb", "sqlite", "redis", "memcached", "kafka", "rabbitmq",
    "elasticsearch", "opensearch", "mongodb", "cassandra", "dynamodb", "s3", "gcs", "nginx", "envoy",
    "clickhouse", "snowflake", "bigquery", "pgbouncer", "etcd", "consul", "vault", "minio", "celery",
)
KNOWN_TOOLS = (
    "docker", "kubernetes", "kubectl", "helm", "terraform", "ansible", "git", "github actions", "gitlab ci",
    "jenkins", "pytest", "ruff", "mypy", "eslint", "prettier", "webpack", "vite", "npm", "pnpm",
    "yarn", "pip", "poetry", "cargo", "maven", "gradle", "bazel", "alembic", "sqlalchemy", "k8s",
    "django", "flask", "fastapi", "react", "next.js", "node.js", "nodejs", "python", "typescript", "golang",
    "playwright", "selenium", "grafana", "prometheus", "opentelemetry", "sentry", "datadog",
)
BUILTIN_ALIASES = {"postgresql": "postgres", "k8s": "kubernetes", "nodejs": "node.js", "golang": "go",
                   "mongo": "mongodb", "elastic": "elasticsearch"}
_ORG_SUFFIX = re.compile(r"\b(?:Inc|Corp|Corporation|LLC|Ltd|GmbH|Labs|Group|Company|Co|Foundation|University|"
                         r"Institute|Bank|Agency|Ministry|Department)\.?$")
_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.? ([A-Z][a-z]+(?: [A-Z][a-z]+)?)")
_FILE_EXT = ("py|pyi|js|jsx|ts|tsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift|scala|sql|sh|bash|ps1|"
             "yaml|yml|json|toml|ini|cfg|conf|md|rst|txt|lock|tf|proto|graphql|html|css|scss|env")
_PATTERNS = (
    ("error", re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning|Fault|Timeout))\b")),
    ("error", re.compile(r"\b((?:E|ERR|TS|SQLSTATE[ ]?)\d{3,5})\b")),
    ("error", re.compile(r"\b(HTTP[ ]?[45]\d\d|(?:status|code)[ ]?[45]\d\d)\b", re.I)),
    ("file", re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:" + _FILE_EXT + r"))\b")),
    ("symbol", re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)\(\)")),
    ("symbol", re.compile(r"\b([a-z_][a-z0-9_]{2,})\(\)")),
    ("symbol", re.compile(r"`([A-Za-z_][\w.:]{2,})`")),
    ("concept", re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]{2,})\b")),  # environment variables, constants
)
_PROPER = re.compile(r"\b([A-Z][a-z]+(?:[ -](?:[A-Z][a-z]+|of|de|von|van|the))*(?: [A-Z][a-z]+)?)\b")
_ACRONYM = re.compile(r"\b([A-Z]{2,6}s?)\b")
_COMMON = frozenset("""
a an the this that these those it its i we you he she they my our your their his her there here
when where what which who why how if then else also but and or not no yes so as at by for from in
into of on onto to with without after before during while until since about above below over under
use using used run running set get make add remove fix check see note never always only all any each
every some most more less first last next new old true false none null todo note warning error info
debug monday tuesday wednesday thursday friday saturday sunday january february march april may june
july august september october november december today tomorrow yesterday please thanks hi hello ok
rule why how apply counter examples step steps example
""".split())
_ACRONYM_STOP = frozenset({"OK", "ID", "IDS", "TODO", "FIXME", "NOTE", "AND", "OR", "NOT", "THE", "A", "I",
                           "AM", "PM", "UTC", "USA", "API", "APIS", "URL", "URLS", "JSON", "YAML", "HTML",
                           "CSS", "SQL", "CLI", "UI", "CI", "CD", "PR", "PRS", "IO", "OS", "VM", "CPU",
                           "GPU", "RAM", "HTTP", "HTTPS", "TCP", "UDP", "DNS", "SSH", "TLS", "SSL"})
MAX_TEXT = 200_000


@dataclass(frozen=True)
class Mention:
    """One entity a text mentions."""
    name: str          # surface form as written
    type: str          # ontology entity type
    key: str           # canonical node id, e.g. "service:postgres"
    start: int = -1
    end: int = -1
    source: str = "pattern"  # pattern | spacy | gazetteer

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "key": self.key, "start": self.start, "end": self.end,
                "source": self.source}


def slug(text: str) -> str:
    """The normalised form two spellings of one entity share: lower case, words joined
    by underscores, a possessive or plural `s` on a multi-letter word dropped."""
    text = re.sub(r"['’]s\b", "", (text or "").strip().lower())
    text = re.sub(r"[^a-z0-9.+#/]+", "_", text).strip("_./")
    return text[:120]


def canonical_key(name: str, type_: str, onto=None) -> str:
    key = f"{type_}:{slug(name)}"
    return onto.canonical_id(key) if onto is not None else key


# --- spaCy (optional) --------------------------------------------------------------

_NLP = None
_NLP_LOCK = threading.Lock()
_NLP_TRIED = False


def spacy_model():
    """The spaCy pipeline, or None when spaCy or its model is not installed (or
    COMMONTRACE_SPACY_MODEL is "none")."""
    global _NLP, _NLP_TRIED
    with _NLP_LOCK:
        if _NLP_TRIED:
            return _NLP
        _NLP_TRIED = True
        name = os.environ.get("COMMONTRACE_SPACY_MODEL", "en_core_web_sm").strip()
        if not name or name.lower() == "none":
            return None
        try:
            import spacy

            _NLP = spacy.load(name, disable=["lemmatizer", "textcat"])
        except Exception:  # noqa: BLE001 - spaCy missing, or the model not downloaded
            _NLP = None
        return _NLP


def backend() -> str:
    return "spacy" if spacy_model() is not None else "patterns"


# --- extraction --------------------------------------------------------------------

def _gazetteer(text: str) -> list[tuple[str, str, int, int]]:
    out = []
    lowered = text.lower()
    for type_, names in (("service", KNOWN_SERVICES), ("tool", KNOWN_TOOLS)):
        for name in names:
            for m in re.finditer(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", lowered):
                out.append((BUILTIN_ALIASES.get(name, text[m.start():m.end()]), type_, m.start(), m.end()))
    return out


def _pattern_mentions(text: str, onto_aliases: dict[str, str]) -> list[tuple[str, str, int, int, str]]:
    found: list[tuple[str, str, int, int, str]] = []
    for name, type_, s, e in _gazetteer(text):
        found.append((name, type_, s, e, "gazetteer"))
    for type_, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.group(1), type_, m.start(1), m.end(1), "pattern"))
    lowered = text.lower()
    for alias, canonical in onto_aliases.items():
        for m in re.finditer(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", lowered):
            found.append((text[m.start():m.end()], canonical.split(":", 1)[0], m.start(), m.end(), "gazetteer"))
    for m in _PROPER.finditer(text):
        name = m.group(1)
        words = name.split()
        if words[0].lower() in _COMMON:
            words = words[1:]
            if not words:
                continue
            name = " ".join(words)
        if len(words) == 1 and (len(name) < 3 or name.lower() in _COMMON):
            continue
        start = m.end(1) - len(name)
        sentence_start = start == 0 or text[max(0, start - 2):start].strip() in (".", "!", "?", "\n", "#", "-", "*")
        if len(words) == 1 and sentence_start:
            continue  # a capitalised first word is usually just a sentence opener
        found.append((name, "organization" if _ORG_SUFFIX.search(name) else "concept", start, m.end(1),
                      "pattern"))
    for m in _HONORIFIC.finditer(text):
        found.append((m.group(1), "person", m.start(), m.end(), "gazetteer"))
    for m in _ACRONYM.finditer(text):
        if m.group(1).rstrip("s") not in _ACRONYM_STOP and m.group(1) not in _ACRONYM_STOP:
            found.append((m.group(1), "concept", m.start(1), m.end(1), "pattern"))
    return found


def _spacy_mentions(doc) -> list[tuple[str, str, int, int, str]]:
    out = []
    for ent in doc.ents:
        type_ = SPACY_LABELS.get(ent.label_)
        if type_:
            out.append((ent.text.strip(), type_, ent.start_char, ent.end_char, "spacy"))
    return out


_PRIORITY = {"gazetteer": 0, "spacy": 1, "pattern": 2}
_TYPE_RANK = {"error": 0, "file": 1, "symbol": 2, "service": 3, "tool": 4, "person": 5, "organization": 6,
              "place": 7, "event": 8, "concept": 9}


def _ontology_type(name: str, text: str, start: int, end: int, onto, embedder) -> str | None:
    """A declared type for an untyped (fallback) mention, from the ontology's aliases and
    keywords, and from embeddings when an embedder is given; None when unknown."""
    from commontrace import ontology_classify

    methods = ("keyword", "embedding") if embedder is not None else ("keyword",)
    found = ontology_classify.classify(name, text[max(0, start - 80):end + 40], onto=onto, embedder=embedder,
                                       methods=methods)
    return found.type


def _resolve(text: str, raw: list[tuple[str, str, int, int, str]], onto, embedder=None) -> list[Mention]:
    """Overlapping spans keep the most specific reading; one key per entity, first
    occurrence kept. A mention left at the fallback type is classified into the
    ontology's types (`ontology_classify`) when its name or context says which."""
    raw = sorted(raw, key=lambda r: (r[2], -(r[3] - r[2]), _PRIORITY[r[4]], _TYPE_RANK.get(r[1], 9)))
    taken: list[tuple[int, int]] = []
    by_key: dict[str, Mention] = {}
    for name, type_, s, e, source in raw:
        if any(s < te and ts < e for ts, te in taken):
            continue
        type_ = onto.entity_type(type_) if onto is not None else type_
        if onto is not None and type_ == FALLBACK_TYPE:
            type_ = _ontology_type(name, text, s, e, onto, embedder) or type_
        key = canonical_key(name, type_, onto)
        if not key.split(":", 1)[-1]:
            continue
        taken.append((s, e))
        by_key.setdefault(key, Mention(name, key.split(":", 1)[0], key, s, e, source))
    return sorted(by_key.values(), key=lambda m: m.start)


def _alias_map(onto) -> dict[str, str]:
    out: dict[str, str] = {}
    if onto is None:
        return out
    for canonical, variants in onto.entity_aliases.items():
        if ":" not in canonical:
            continue
        for v in (*variants, canonical.split(":", 1)[1].replace("_", " ")):
            if len(v) >= 2:
                out[v.lower()] = canonical
    return out


def extract(text: str, *, onto=None, use_spacy: bool | None = None, embedder=None) -> list[Mention]:
    """Entities in `text`, each once, in order of first mention. With an ontology, untyped
    mentions are classified into its types; `embedder` adds semantic classification."""
    return extract_batch([text], onto=onto, use_spacy=use_spacy, embedder=embedder)[0]


def extract_batch(texts: Iterable[str], *, onto=None, use_spacy: bool | None = None,
                  embedder=None) -> list[list[Mention]]:
    """`extract` over many texts; spaCy (when used) processes them as one stream."""
    texts = [(t or "")[:MAX_TEXT] for t in texts]
    nlp = spacy_model() if use_spacy is not False else None
    if use_spacy and nlp is None:
        raise RuntimeError("spaCy extraction was requested but spaCy or its model is not installed "
                           "(pip install spacy && python -m spacy download en_core_web_sm)")
    docs = list(nlp.pipe(texts, batch_size=32)) if nlp is not None else [None] * len(texts)
    aliases = _alias_map(onto)
    out = []
    for text, doc in zip(texts, docs):
        raw = _pattern_mentions(text, aliases)
        if doc is not None:
            raw += _spacy_mentions(doc)
        out.append(_resolve(text, raw, onto, embedder))
    return out


# --- the entity store (graph nodes) ----------------------------------------------------

def _lesson_files(root: str) -> list[str]:
    base = os.path.join(paths.memory_dir(root), "lessons")
    if not os.path.isdir(base):
        return []
    return sorted(os.path.join(base, f) for f in os.listdir(base)
                  if f.startswith("lesson_") and f.endswith(".md") and f != "lesson_template.md")


def _lesson_text(path: str) -> tuple[str, str]:
    from commontrace import frontmatter

    fm, body = frontmatter.read(path)
    slug_ = str(fm.get("name") or os.path.basename(path)[:-3])
    tags = fm.get("tags") or []
    parts = [str(fm.get("name") or slug_), str(fm.get("description") or ""), str(fm.get("applies_when") or ""),
             " ".join(str(t) for t in tags if isinstance(t, str)), body]
    return slug_, "\n".join(parts)


def link_lessons(root: str, slugs: list[str] | None = None, *, use_spacy: bool | None = None,
                 min_mentions: int = 1, embedder=None) -> dict:
    """Extract entities from lessons and record them in the graph: one node per
    canonical entity (its surface forms kept as aliases, its mention count as weight)
    and a `mentions` edge from each lesson to each entity it names. Re-running is
    idempotent; an entity a lesson no longer names loses its edge."""
    from commontrace import graph, ontology

    onto = ontology.load(root)
    files = _lesson_files(root)
    if slugs:
        wanted = set(slugs)
        files = [f for f in files if os.path.basename(f)[:-3] in wanted]
    texts, names = [], []
    for path in files:
        try:
            name, text = _lesson_text(path)
        except Exception:  # noqa: BLE001 - one unreadable lesson does not stop linking
            continue
        names.append(name)
        texts.append(text)
    mentions = extract_batch(texts, onto=onto, use_spacy=use_spacy, embedder=embedder)
    counts: dict[str, int] = {}
    for found in mentions:
        for m in found:
            counts[m.key] = counts.get(m.key, 0) + 1
    edges = nodes = retired = 0
    with graph.batch(root) as txn:
        for name, found in zip(names, mentions):
            lesson_id = f"lesson:{name}"
            if lesson_id not in txn.nodes:
                graph._put_node(txn, lesson_id, "lesson", name, None, {"source": "entities.link"})
            keep = {m.key for m in found if counts[m.key] >= min_mentions}
            for m in found:
                if m.key not in keep:
                    continue
                node = txn.nodes.get(m.key)
                if node is None:
                    nodes += 1
                variants = sorted({*(node.properties.get("aliases", []) if node else []), m.name})[:20]
                graph._put_node(txn, m.key, m.type, m.name if node is None else "",
                                {"aliases": variants, "mentions": counts[m.key]}, None)
                key = (lesson_id, m.key, "mentions")
                if not any(e.invalid_at is None for e in txn.by_key.get(key, [])):
                    graph.add_edge(root, lesson_id, m.key, "mentions",
                                   properties={"extracted_by": m.source}, provenance={"source": "entities.link"})
                    edges += 1
            for edge in txn.by_source_relation.get((lesson_id, "mentions"), []):
                if edge.invalid_at is None and edge.target not in keep and \
                        (edge.properties or {}).get("extracted_by"):
                    graph._close(edge, graph._now(), graph._now(), "no longer mentioned")
                    txn.edges_dirty = True
                    retired += 1
    return {"lessons": len(names), "entities": len(counts), "new_entities": nodes, "new_links": edges,
            "retired_links": retired, "backend": backend() if use_spacy is not False else "patterns"}


def entities(root: str, *, type_: str | None = None, limit: int = 100) -> list[dict]:
    """Known entities, most mentioned first."""
    from commontrace import graph

    out = []
    for node in graph.load_nodes(root).values():
        if node.entity_type == "lesson" or (type_ and node.entity_type != type_) or node.is_forgotten:
            continue
        out.append({"id": node.id, "name": node.name, "type": node.entity_type,
                    "mentions": int((node.properties or {}).get("mentions", 0) or 0),
                    "aliases": (node.properties or {}).get("aliases", [])})
    return sorted(out, key=lambda e: (-e["mentions"], e["id"]))[:max(1, limit)]


def merge(root: str, keep: str, duplicate: str, *, evidence: dict | None = None,
          provenance: dict | None = None) -> dict:
    """Fold `duplicate` into `keep`: its edges move over, its names become aliases, and
    it is recorded as superseded so the history is not lost. `evidence` (why they are
    one, e.g. a resolver's method and score) is kept on both the duplicate and the
    `supersedes` edge; `provenance` is recorded for the merge's node and edge writes."""
    from commontrace import graph

    keep_id, dup_id = graph._clean_id(keep), graph._clean_id(duplicate)
    if keep_id == dup_id:
        raise ValueError("an entity cannot be merged into itself")
    moved = 0
    with graph.batch(root) as txn:
        if keep_id not in txn.nodes or dup_id not in txn.nodes:
            raise ValueError(f"unknown entity: {keep_id if keep_id not in txn.nodes else dup_id}")
        winner, loser = txn.nodes[keep_id], txn.nodes[dup_id]
        aliases = sorted({*winner.properties.get("aliases", []), *loser.properties.get("aliases", []),
                          loser.name})[:40]
        mentions = int(winner.properties.get("mentions", 0) or 0) + int(loser.properties.get("mentions", 0) or 0)
        graph._put_node(txn, keep_id, winner.entity_type, "", {"aliases": aliases, "mentions": mentions},
                        provenance)
        for edge in list(txn.edges):
            if edge.invalid_at is not None or dup_id not in (edge.source, edge.target):
                continue
            src = keep_id if edge.source == dup_id else edge.source
            dst = keep_id if edge.target == dup_id else edge.target
            graph._close(edge, graph._now(), graph._now(), f"merged into {keep_id}")
            if src != dst:
                graph.add_edge(root, src, dst, edge.relation, weight=edge.weight, valid_at=edge.valid_at,
                               properties={**(edge.properties or {}), "merged_from": dup_id}, provenance=provenance)
            moved += 1
        reason = {"reason": "entity merge", **({"evidence": evidence} if evidence else {})}
        graph.add_edge(root, keep_id, dup_id, "supersedes", properties=reason, provenance=provenance)
        loser.properties = {**loser.properties, "merged_into": keep_id, **({"merge_evidence": evidence}
                                                                           if evidence else {})}
        txn.nodes_dirty = True
    return {"kept": keep_id, "merged": dup_id, "edges_moved": moved}


def duplicates(root: str, *, limit: int = 50) -> list[dict]:
    """Entity pairs that look like one thing: same slug across types, or one name is
    an alias, acronym or plural of the other."""
    from commontrace import graph

    nodes = [n for n in graph.load_nodes(root).values()
             if n.entity_type != "lesson" and not n.is_forgotten and not n.properties.get("merged_into")]
    by_norm: dict[str, list] = {}
    for n in nodes:
        name = slug(n.name).rstrip("s") if len(n.name) > 3 else slug(n.name)
        for form in {name, *(slug(a) for a in n.properties.get("aliases", []))}:
            by_norm.setdefault(form, []).append(n)
        words = [w for w in re.split(r"[_ -]+", slug(n.name)) if w]
        if len(words) >= 2:
            by_norm.setdefault("".join(w[0] for w in words), []).append(n)
    pairs, seen = [], set()
    for form, group in by_norm.items():
        ids = sorted({n.id for n in group})
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if (a, b) not in seen:
                    seen.add((a, b))
                    pairs.append({"a": a, "b": b, "because": form})
    return pairs[:limit]
