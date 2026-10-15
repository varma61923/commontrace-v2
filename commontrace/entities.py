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
from commontrace.configuration import getenv as _configuration_getenv

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
        name = _configuration_getenv("COMMONTRACE_SPACY_MODEL", "en_core_web_sm").strip()
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

_LETTER_UP = "A-ZÀ-Þ"
_NAME_WORD = r"[" + _LETTER_UP + r"][\w'À-ɏ-]*"
_CONNECTOR = r"(?:of(?: the)?|de|da|du|del|della|la|le|von|van|der|den|y)"
# Capitalised words (inner capitals allowed: OpenAI, KubeCon), joined by spaces, hyphens
# and name connectors ("Bank of the West", "Rio de Janeiro").
_PROPER = re.compile(r"(?<![\w.@/$'-])(" + _NAME_WORD + r"(?:[ -](?:" + _CONNECTOR + r" )?" + _NAME_WORD
                     + r")*)(?![\w@/$-])")
_TOKEN = re.compile(r"[\wÀ-ɏ][\wÀ-ɏ.'+#:-]*[\w+#]|[\wÀ-ɏ]")
_TOOL_OBJECT = re.compile(r"\b(?i:run|runs|ran|running|use|uses|used|using|install|installs|installed|installing|"
                          r"invoke|invoked|via|upgrade|upgraded)\s+([a-z][a-z0-9_.+-]{2,30}[a-z0-9])\b")
_SENTENCE_OPENERS = (".", "!", "?", "\n", ":", ";", '"', "(", "-", "*", "#", ">")


def _lexicon_index() -> tuple[dict[str, str], dict[str, list[tuple[tuple[str, ...], str]]]]:
    from commontrace import ontology_lexicon

    single: dict[str, str] = {}
    multi: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    lists = [*ontology_lexicon.LISTS.items(), ("service", frozenset(KNOWN_SERVICES)),
             ("tool", frozenset(KNOWN_TOOLS))]
    for type_, names in lists:
        if type_ == "person":
            continue  # first names type people through the classifier, not as whole mentions
        for name in names:
            words = tuple(name.split())
            if len(words) == 1:
                single.setdefault(name, type_)
            else:
                multi.setdefault(words[0], []).append((words, type_))
    for entries in multi.values():
        entries.sort(key=lambda e: -len(e[0]))
    return single, multi


_LEXICON: tuple | None = None
_CAPITALISED_TYPES = frozenset({"person", "place", "organization"})


def _lexicon_mentions(text: str) -> list[tuple[str, str, int, int, str]]:
    """Well-known names by one pass over the tokens (no regex per name). People,
    places and organizations must be written capitalised, and ambiguous names
    ("black", "target") count only then."""
    global _LEXICON
    from commontrace import ontology_lexicon

    if _LEXICON is None:
        _LEXICON = _lexicon_index()
    single, multi = _LEXICON
    tokens = [(m.group(0).rstrip(".:'"), m.start()) for m in _TOKEN.finditer(text)]
    out = []
    i = 0
    while i < len(tokens):
        word, start = tokens[i]
        lowered = word.lower()
        matched = False
        for words, type_ in multi.get(lowered, ()):
            span = tokens[i:i + len(words)]
            if len(span) == len(words) and tuple(w.lower() for w, _s in span) == words:
                end = span[-1][1] + len(span[-1][0])
                if type_ not in _CAPITALISED_TYPES or word[:1].isupper():
                    out.append((text[start:end], type_, start, end, "gazetteer"))
                    i += len(words)
                    matched = True
                    break
        if matched:
            continue
        type_ = single.get(lowered)
        capital = word[:1].isupper()
        if type_ and (capital or (type_ not in _CAPITALISED_TYPES and lowered not in ontology_lexicon.AMBIGUOUS)):
            name = BUILTIN_ALIASES.get(lowered, word)
            out.append((name, type_, start, start + len(word), "gazetteer"))
        i += 1
    return out


def _shape_patterns() -> list[tuple[str, re.Pattern[str]]]:
    from commontrace import ontology_lexicon

    out = []
    for type_, _score, pattern in ontology_lexicon.SHAPES:
        body = pattern.pattern.removeprefix("^").removesuffix("$")
        if type_ == "person" and "Dr" in body:
            continue  # titled names are found by _HONORIFIC, keyed without the title
        out.append((type_, re.compile(r"(?<![\w@./$-])(" + body + r")(?![\w@/$-]|\.\w)", pattern.flags)))
    return out


_SHAPES: list[tuple[str, re.Pattern[str]]] | None = None


def _head_pattern(onto) -> re.Pattern[str] | None:
    """Noun phrases ending in a word that names a declared type: "March outage",
    "the billing service", "Platform team"."""
    if onto is None:
        return None
    from commontrace import ontology_classify

    heads = sorted({" ".join(p) for t in ontology_classify.candidate_types(onto)
                    for p in ontology_classify._vocabulary(onto, t) if len(p) == 1 and len(p[0]) > 2},
                   key=len, reverse=True)
    key = tuple(heads)
    cached = _HEAD_PATTERNS.get(key)
    if cached is None:
        alternation = "|".join(re.escape(h) for h in heads)
        cached = re.compile(r"(?:\b(?:the|our|their|a|an|this|that|its)\s+((?:[a-z][\w-]*\s+){1,2}(?:" + alternation
                            + r"))|\b((?:" + _NAME_WORD + r"\s+){1,3}(?:" + alternation + r")))\b")
        if len(_HEAD_PATTERNS) >= 8:
            _HEAD_PATTERNS.pop(next(iter(_HEAD_PATTERNS)))
        _HEAD_PATTERNS[key] = cached
    return cached


_HEAD_PATTERNS: dict[tuple, re.Pattern[str]] = {}


def _trim(text: str, name: str, start: int) -> tuple[str, int, bool] | None:
    """`name` without leading ordinary words ("Follow the Style Guide" is "Style Guide"),
    trailing connectors or a possessive; and whether it opens a sentence. None if
    nothing name-like is left."""
    from commontrace import ontology_lexicon

    words = name.split(" ")
    opener = start == 0 or text[max(0, start - 2):start].strip()[-1:] in _SENTENCE_OPENERS
    # Only a sentence's first word is capitalised for grammar: mid-sentence "the Search
    # API" keeps "Search", while "Follow the Style Guide" drops "Follow".
    while opener and words and (words[0].lower() in ontology_lexicon.COMMON_WORDS or words[0].lower() in _COMMON):
        start += len(words[0]) + 1
        words = words[1:]
    while words and words[-1].lower() in ("of", "the", "de", "von", "van", "la", "le", "y"):
        words = words[:-1]
    if not words:
        return None
    name = re.sub(r"['’]s$", "", " ".join(words))
    opener = start == 0 or text[max(0, start - 2):start].strip()[-1:] in _SENTENCE_OPENERS
    return name, start, opener


def _pattern_mentions(text: str, onto_aliases: dict[str, str], onto=None) -> list[tuple[str, str, int, int, str]]:
    """Candidate mentions with a provisional type and their source: ``alias`` (the
    ontology's), ``gazetteer`` (built-in names), ``shape`` (code and identifier forms),
    ``pattern`` (capitalised names, noun phrases with a type's head word) and ``weak``
    (a sentence-initial word, a word after "run"/"use"); a weak candidate is kept only
    if the classifier gives it a declared type."""
    from commontrace import ontology_lexicon

    global _SHAPES
    if _SHAPES is None:
        _SHAPES = _shape_patterns()
    found: list[tuple[str, str, int, int, str]] = list(_lexicon_mentions(text))
    for type_, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.group(1), type_, m.start(1), m.end(1), "pattern"))
    for type_, pattern in _SHAPES:
        for m in pattern.finditer(text):
            found.append((m.group(1), type_, m.start(1), m.end(1), "shape"))
    lowered = text.lower()
    for alias, canonical in onto_aliases.items():
        for m in re.finditer(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", lowered):
            found.append((text[m.start():m.end()], canonical.split(":", 1)[0], m.start(), m.end(), "alias"))
    for m in _PROPER.finditer(text):
        trimmed = _trim(text, m.group(1), m.start(1))
        if trimmed is None:
            continue
        name, start, opener = trimmed
        words = name.split()
        if len(words) == 1 and (len(name) < 2 or name.lower() in _COMMON
                                or name.lower() in ontology_lexicon.COMMON_WORDS):
            continue
        source = "weak" if len(words) == 1 and opener else "pattern"
        found.append((name, "organization" if _ORG_SUFFIX.search(name) else "concept", start, start + len(name),
                      source))
    for m in _HONORIFIC.finditer(text):
        found.append((m.group(1), "person", m.start(1), m.end(1), "gazetteer"))
    for m in _ACRONYM.finditer(text):
        if m.group(1).rstrip("s") not in _ACRONYM_STOP and m.group(1) not in _ACRONYM_STOP:
            found.append((m.group(1), "concept", m.start(1), m.end(1), "pattern"))
    for m in _TOOL_OBJECT.finditer(text):
        word = m.group(1)
        if word not in ontology_lexicon.COMMON_WORDS and word not in _COMMON:
            found.append((word, "concept", m.start(1), m.end(1), "weak"))
    heads = _head_pattern(onto)
    if heads is not None:
        for m in heads.finditer(text):
            group = 1 if m.group(1) else 2
            trimmed = _trim(text, m.group(group), m.start(group))
            if trimmed is None or " " not in trimmed[0]:
                continue
            name, start, _opener = trimmed
            found.append((name, "concept", start, start + len(name), "weak" if group == 1 else "pattern"))
    return found


def _spacy_mentions(doc) -> list[tuple[str, str, int, int, str]]:
    out = []
    for ent in doc.ents:
        type_ = SPACY_LABELS.get(ent.label_)
        if type_:
            out.append((ent.text.strip(), type_, ent.start_char, ent.end_char, "spacy"))
    return out


_PRIORITY = {"alias": 0, "gazetteer": 1, "shape": 1, "spacy": 2, "pattern": 3, "weak": 4}
_TYPE_RANK = {"error": 0, "file": 1, "symbol": 2, "service": 3, "tool": 4, "person": 5, "organization": 6,
              "place": 7, "event": 8, "concept": 9}


def _classify(name: str, text: str, start: int, end: int, onto, embedder, exemplars=None):
    """The ontology classification of one candidate in its context window."""
    from commontrace import ontology_classify

    methods = ("keyword", "embedding") if embedder is not None else ("keyword",)
    return ontology_classify.classify(name, text[max(0, start - 120):end + 80], onto=onto, embedder=embedder,
                                      methods=methods, exemplars=exemplars)


def _resolve(text: str, raw: list[tuple[str, str, int, int, str]], onto, embedder=None,
             exemplars=None) -> list[Mention]:
    """Overlapping spans keep the most specific reading; one key per entity, first
    occurrence kept. With an ontology every candidate but a declared alias is typed by
    `ontology_classify` (its own evidence included), a weak candidate the classifier
    cannot type is dropped, and an untyped strong one keeps its provisional type."""
    raw = sorted(raw, key=lambda r: (r[2], -(r[3] - r[2]), _PRIORITY[r[4]], _TYPE_RANK.get(r[1], 9)))
    taken: list[tuple[int, int]] = []
    by_key: dict[str, Mention] = {}
    for name, type_, s, e, source in raw:
        if any(s < te and ts < e for ts, te in taken):
            continue
        if onto is None and source == "weak":
            continue
        type_ = onto.entity_type(type_) if onto is not None else type_
        if onto is not None and source != "alias":
            found = _classify(name, text, s, e, onto, embedder, exemplars)
            if found.known:
                type_ = found.type
            elif source == "weak":
                continue
        key = canonical_key(name, type_, onto)
        if not key.split(":", 1)[-1]:
            continue
        taken.append((s, e))
        by_key.setdefault(key, Mention(name, key.split(":", 1)[0], key, s, e,
                                       "gazetteer" if source in ("alias", "gazetteer") else
                                       "spacy" if source == "spacy" else "pattern"))
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


def extract(text: str, *, onto=None, use_spacy: bool | None = None, embedder=None,
            exemplars=None) -> list[Mention]:
    """Entities in `text`, each once, in order of first mention. With an ontology,
    mentions are classified into its types (`ontology_classify`); `embedder` adds
    semantic classification and `exemplars` the store's earlier typings."""
    return extract_batch([text], onto=onto, use_spacy=use_spacy, embedder=embedder, exemplars=exemplars)[0]


def extract_batch(texts: Iterable[str], *, onto=None, use_spacy: bool | None = None,
                  embedder=None, exemplars=None) -> list[list[Mention]]:
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
        raw = _pattern_mentions(text, aliases, onto)
        if doc is not None:
            raw += _spacy_mentions(doc)
        out.append(_resolve(text, raw, onto, embedder, exemplars))
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
    from commontrace import ontology_classify

    mentions = extract_batch(texts, onto=onto, use_spacy=use_spacy, embedder=embedder,
                             exemplars=ontology_classify.Exemplars.from_graph(root))
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
