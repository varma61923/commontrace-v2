"""Fact-cluster consolidation: which atomic facts say the same thing, and what one record says it best.

`observations.consolidate_facts` maps reinforced facts one-to-one onto
observations; `consolidate.build_report` fuses lessons only. This module finds
clusters of near-duplicate and paraphrased *facts*:

1. Facts are read through `hierarchical.list_facts` (active, unexpired, not
   forgotten, proof-eligible) and grouped by their exact scope set: two facts
   are only ever compared inside the same scope, because scope is an
   authorization boundary.
2. Inside a group, statements are normalized (lowercase, punctuation and
   stopwords dropped, Porter-stemmed) and paired by token-set Jaccard. Small
   groups are compared exhaustively; from ``LSH_MIN_ITEMS`` facts on, MinHash
   (`overlap.minhash`) banded LSH (`redundancy._bands_for`) proposes the
   candidate pairs and the exact Jaccard confirms them.
3. With an embedder (any `commontrace.embeddings` tag, or a provider object),
   pairs whose cosine reaches ``embed_threshold`` are linked too, which catches
   paraphrases with little shared vocabulary.
4. Linked facts form clusters (union-find). Each cluster reports a canonical
   representative (most confirmations, then most recent, then smallest id),
   its member ids, the union of their evidence, the pairs that linked them, and
   an optional summary: extractive (the canonical statement, its distinct
   variants and the entities they name) or written by the configured model.

The default is a report. ``apply`` writes one review-status proposal per
cluster through `memory_control` (kind ``proposal``, record id = cluster id,
``data.sources`` = member fact ids), so a human approves or rejects it like any
other proposal. Facts are never edited, superseded or deleted here; a rejected
cluster is not proposed again while its members are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from commontrace import hierarchical, redundancy
from commontrace._lexical import STOPWORDS
from commontrace._stem import stem
from commontrace.overlap import DEFAULT_NUM_PERM, minhash

DEFAULT_THRESHOLD = 0.5
DEFAULT_EMBED_THRESHOLD = 0.9
LSH_MIN_ITEMS = 200
MAX_EMBED_GROUP = 2000  # exact cosine is quadratic; larger groups use the lexical arm only
MAX_EVIDENCE = 200
MAX_VARIANTS = 3
EMBEDDER_ENV = "COMMONTRACE_CONSOLIDATE_EMBEDDER"
CLUSTER_PREFIX = "fcl-"

_WORD = re.compile(r"[a-z0-9]+")
_ENTITY = re.compile(r"\b[A-Z][A-Za-z0-9&'-]*(?:\s+[A-Z][A-Za-z0-9&'-]*)*")
_NOT_ENTITIES = frozenset({"The", "A", "An", "I", "We", "They", "He", "She", "It", "This", "That", "User",
                           "Users", "My", "Our", "Their", "His", "Her"})

SUMMARY_PROMPT = """These atomic facts were recorded separately but say the same thing.
Write ONE sentence that states what they all say, keeping every name, number and date
they agree on. Do not add anything they do not say. If they disagree, state only what
they share.

Facts:
{facts}

Consolidated statement:"""


@dataclass
class FactCluster:
    id: str
    scopes: list[str]
    canonical: str
    statement: str
    members: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    links: list[dict[str, Any]]
    summary: str = ""
    summary_method: str = ""
    entities: list[str] = field(default_factory=list)

    @property
    def member_ids(self) -> list[str]:
        return [m["id"] for m in self.members]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "member_ids": self.member_ids, "size": len(self.members)}


def normalize(statement: str) -> str:
    """The comparable form of a statement: stemmed content words, in order."""
    words = _WORD.findall((statement or "").lower())
    return " ".join(stem(w) for w in words if w not in STOPWORDS and len(w) > 1)


def _tokens(normalized: str) -> frozenset[str]:
    return frozenset(normalized.split())


def _lexical_pairs(norms: Sequence[str], threshold: float) -> dict[tuple[int, int], float]:
    tokens = [_tokens(n) for n in norms]
    if len(norms) < LSH_MIN_ITEMS:
        candidates = ((i, j) for i in range(len(norms)) for j in range(i + 1, len(norms)))
    else:
        bands, rows = redundancy._bands_for(threshold, DEFAULT_NUM_PERM)
        buckets: dict[tuple[int, tuple[int, ...]], list[int]] = defaultdict(list)
        for index, text in enumerate(norms):
            if not tokens[index]:
                continue
            signature = minhash(text, num_perm=DEFAULT_NUM_PERM)
            for band in range(bands):
                buckets[(band, tuple(signature[band * rows:(band + 1) * rows]))].append(index)
        seen: set[tuple[int, int]] = set()
        for members in buckets.values():
            for pos, i in enumerate(members):
                for j in members[pos + 1:]:
                    seen.add((i, j) if i < j else (j, i))
        candidates = iter(sorted(seen))
    out: dict[tuple[int, int], float] = {}
    for i, j in candidates:
        score = redundancy.jaccard(tokens[i], tokens[j])
        if score >= threshold:
            out[(i, j)] = round(score, 4)
    return out


def _resolve_embedder(embedder) -> tuple[Any, str]:
    """(provider or None, tag) from a tag, a provider object, or the environment."""
    if embedder is None:
        embedder = os.environ.get(EMBEDDER_ENV, "").strip() or None
    if embedder is None or embedder in ("", "none", "off"):
        return None, ""
    if isinstance(embedder, str):
        from commontrace import embeddings

        return embeddings.provider(embedder), embedder
    spec = getattr(embedder, "spec", None)
    return embedder, str(getattr(spec, "tag", "") or type(embedder).__name__)


def _embedding_pairs(provider, statements: Sequence[str], threshold: float) -> dict[tuple[int, int], float]:
    vectors = provider.embed(list(statements), query=False)
    if len(vectors) != len(statements):
        raise ValueError("the embedder returned a different number of vectors than statements")
    unit = []
    for vector in vectors:
        values = [float(x) for x in vector]
        norm = math.sqrt(sum(x * x for x in values))
        unit.append([x / norm for x in values] if norm else values)
    out: dict[tuple[int, int], float] = {}
    for i in range(len(unit)):
        for j in range(i + 1, len(unit)):
            if len(unit[i]) != len(unit[j]):
                raise ValueError("the embedder returned vectors of different dimensions")
            cosine = sum(a * b for a, b in zip(unit[i], unit[j]))
            if cosine >= threshold:
                out[(i, j)] = round(cosine, 4)
    return out


def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _recency(fact: hierarchical.AtomicFact) -> str:
    return fact.updated_at or fact.created_at or fact.valid_from or ""


def _canonical(facts: list[hierarchical.AtomicFact]) -> hierarchical.AtomicFact:
    ordered = sorted(facts, key=lambda f: f.id)
    return sorted(ordered, key=lambda f: (int(f.confirmations or 0), _recency(f)), reverse=True)[0]


def _evidence(facts: list[hierarchical.AtomicFact]) -> list[dict[str, Any]]:
    seen: dict[tuple, dict[str, Any]] = {}
    for fact in facts:
        for trace in fact.source_traces:
            if str(trace).strip():
                key = ("trace", str(trace), "", "support")
                seen.setdefault(key, {"kind": "trace", "source_id": str(trace), "facts": []})["facts"].append(fact.id)
        for receipt in fact.evidence:
            key = (receipt.kind, receipt.source_id, receipt.revision, receipt.polarity)
            seen.setdefault(key, {"kind": receipt.kind, "source_id": receipt.source_id,
                                  "revision": receipt.revision, "polarity": receipt.polarity,
                                  "facts": []})["facts"].append(fact.id)
    rows = [seen[k] for k in sorted(seen)]
    for row in rows:
        row["facts"] = sorted(set(row["facts"]))
    return rows[:MAX_EVIDENCE]


def _entities(statements: Sequence[str]) -> list[str]:
    counts: Counter[str] = Counter()
    for statement in statements:
        found = set()
        for match in _ENTITY.findall(statement or ""):
            words = [w for w in match.split() if w not in _NOT_ENTITIES]
            if words:
                found.add(" ".join(words))
        counts.update(found)
    return [name for name, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]


def extractive_summary(canonical: str, statements: Sequence[str], entities: Sequence[str]) -> str:
    """The canonical statement, its distinct variants, and the entities they name."""
    seen = {normalize(canonical)}
    variants = []
    for statement in statements:
        norm = normalize(statement)
        if norm and norm not in seen:
            seen.add(norm)
            variants.append(statement.strip())
    text = canonical.strip()
    if variants:
        text += " (also recorded as: " + "; ".join(variants[:MAX_VARIANTS])
        text += f"; and {len(variants) - MAX_VARIANTS} more)" if len(variants) > MAX_VARIANTS else ")"
    if entities:
        text += " Entities: " + ", ".join(entities) + "."
    return text


def cluster_facts(
    root: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    embedder=None,
    embed_threshold: float = DEFAULT_EMBED_THRESHOLD,
    scope: str = "",
    summarize: str = "extractive",
    complete: Callable[[str], tuple[str, dict]] | None = None,
    facts: Sequence[hierarchical.AtomicFact] | None = None,
) -> dict[str, Any]:
    """Report clusters of near-duplicate facts. Reads only; see `apply_clusters` to propose them.

    ``summarize`` is ``extractive`` (default), ``model`` (the configured LLM, or
    ``complete``; falls back to extractive with a note when none is configured)
    or ``none``. ``facts`` replaces the store read (for callers that already
    hold a filtered list).
    """
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    if not 0 < embed_threshold <= 1:
        raise ValueError("embed_threshold must be in (0, 1]")
    if summarize not in ("extractive", "model", "none"):
        raise ValueError("summarize must be extractive, model or none")
    provider, tag = _resolve_embedder(embedder)
    pool = list(facts) if facts is not None else hierarchical.list_facts(root, status="active", scope=scope)
    groups: dict[tuple[str, ...], list[hierarchical.AtomicFact]] = defaultdict(list)
    for fact in sorted(pool, key=lambda f: f.id):
        groups[tuple(sorted(fact.scopes))].append(fact)
    notes: list[str] = []
    clusters: list[FactCluster] = []
    model_failed = False
    for scope_key in sorted(groups):
        group = groups[scope_key]
        if len(group) < 2:
            continue
        norms = [normalize(f.statement) for f in group]
        links: dict[tuple[int, int], dict[str, float]] = {}
        for pair, score in _lexical_pairs(norms, threshold).items():
            links.setdefault(pair, {})["jaccard"] = score
        if provider is not None:
            if len(group) > MAX_EMBED_GROUP:
                notes.append(f"scope {list(scope_key)}: {len(group)} facts exceed {MAX_EMBED_GROUP}; "
                             "embedding similarity skipped for this scope")
            else:
                for pair, score in _embedding_pairs(provider, [f.statement for f in group],
                                                    embed_threshold).items():
                    links.setdefault(pair, {})["cosine"] = score
        parent = list(range(len(group)))
        for i, j in links:
            a, b = _find(parent, i), _find(parent, j)
            if a != b:
                parent[max(a, b)] = min(a, b)
        members_of: dict[int, list[int]] = defaultdict(list)
        for i in range(len(group)):
            members_of[_find(parent, i)].append(i)
        for indices in members_of.values():
            if len(indices) < 2:
                continue
            members = [group[i] for i in indices]
            canonical = _canonical(members)
            ids = sorted(f.id for f in members)
            digest = hashlib.sha256(json.dumps([list(scope_key), ids]).encode("utf-8")).hexdigest()[:16]
            index_set = set(indices)
            cluster_links = [{"a": group[i].id, "b": group[j].id, **scores}
                             for (i, j), scores in sorted(links.items()) if i in index_set and j in index_set]
            statements = [f.statement for f in sorted(members, key=lambda f: f.id)]
            entities = _entities(statements)
            summary, method = "", ""
            if summarize == "model" and not model_failed:
                try:
                    summary = _model_summary(statements, complete)
                    method = "model"
                except Exception as exc:  # noqa: BLE001 - a missing or failing model degrades to extractive
                    model_failed = True
                    notes.append(f"model summary unavailable ({type(exc).__name__}: {exc}); used extractive")
            if summarize != "none" and not summary:
                summary, method = extractive_summary(canonical.statement, statements, entities), "extractive"
            clusters.append(FactCluster(
                id=CLUSTER_PREFIX + digest, scopes=list(scope_key), canonical=canonical.id,
                statement=canonical.statement,
                members=[{"id": f.id, "statement": f.statement, "confirmations": int(f.confirmations or 0),
                          "updated_at": _recency(f), "category": f.category}
                         for f in sorted(members, key=lambda f: f.id)],
                evidence=_evidence(members), links=cluster_links, summary=summary, summary_method=method,
                entities=entities))
    clusters.sort(key=lambda c: (-len(c.members), c.id))
    return {
        "facts": len(pool), "scopes": len(groups), "threshold": threshold,
        "embedder": tag or None, "embed_threshold": embed_threshold if provider is not None else None,
        "clusters": [c.to_dict() for c in clusters],
        "redundant_facts": sum(len(c.members) - 1 for c in clusters),
        "notes": notes,
    }


def _model_summary(statements: Sequence[str], complete) -> str:
    if complete is None:
        from commontrace import llm

        complete = llm.complete
    text, _usage = complete(SUMMARY_PROMPT.format(facts="\n".join(f"- {s}" for s in statements)))
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("the model returned an empty summary")
    return text[:2000]


def apply_clusters(root: str, report: dict[str, Any], *, actor: str = "consolidate") -> dict[str, Any]:
    """Write one review-status proposal per reported cluster. Never touches a fact.

    A cluster whose proposal is already in review with the same members is left
    alone; one a reviewer rejected (archived) is not proposed again unless its
    membership changed (which changes the cluster id).
    """
    from commontrace import memory_control

    existing = {r["id"]: r for r in memory_control.records(root, "proposal")}
    written, unchanged, rejected = [], [], []
    for cluster in report.get("clusters", []):
        sources = list(cluster["member_ids"])
        previous = existing.get(cluster["id"])
        if previous is not None:
            status = (previous.get("data") or {}).get("status")
            if status == "archived":
                rejected.append(cluster["id"])
                continue
            if status == "review" and sorted((previous.get("data") or {}).get("sources") or []) == sorted(sources):
                unchanged.append(cluster["id"])
                continue
        text = cluster.get("summary") or cluster["statement"]
        record = memory_control.put(
            root, "proposal", text, record_id=cluster["id"], labels=list(cluster["scopes"]), actor=actor,
            data={"status": "review", "sources": sources, "kind": "fact-cluster",
                  "canonical": cluster["canonical"], "statement": cluster["statement"],
                  "summary_method": cluster.get("summary_method") or "",
                  "entities": list(cluster.get("entities") or []),
                  "evidence": list(cluster.get("evidence") or [])[:50],
                  "links": list(cluster.get("links") or [])[:100],
                  "threshold": report.get("threshold"), "embedder": report.get("embedder")})
        written.append({"id": record["id"], "revision": record["revision"]})
    return {"written": written, "unchanged": unchanged, "skipped_rejected": rejected}


def render(report: dict[str, Any]) -> str:
    """Human-readable report."""
    lines = ["# Fact consolidation report", "",
             f"- Facts considered: **{report['facts']}** in {report['scopes']} scope group(s)",
             f"- Lexical threshold (stemmed token Jaccard): {report['threshold']}"
             + (f" · embedder `{report['embedder']}` at cosine {report['embed_threshold']}"
                if report.get("embedder") else " · no embedder"),
             f"- Clusters: **{len(report['clusters'])}** · redundant facts: **{report['redundant_facts']}**", ""]
    if not report["clusters"]:
        lines.append("No near-duplicate facts found.")
    for cluster in report["clusters"]:
        scope = ", ".join(cluster["scopes"]) or "global"
        lines += [f"## `{cluster['id']}` ({cluster['size']} facts, scope: {scope})",
                  f"Canonical: `{cluster['canonical']}` -- {cluster['statement']}"]
        if cluster.get("summary"):
            lines.append(f"Summary ({cluster['summary_method']}): {cluster['summary']}")
        for member in cluster["members"]:
            mark = "*" if member["id"] == cluster["canonical"] else "-"
            lines.append(f"  {mark} `{member['id']}` x{member['confirmations']}: {member['statement']}")
        if cluster["evidence"]:
            lines.append(f"  evidence: {len(cluster['evidence'])} source(s)")
        lines.append("")
    for note in report.get("notes") or []:
        lines.append(f"Note: {note}")
    if report.get("applied"):
        applied = report["applied"]
        lines += ["", f"Proposed {len(applied['written'])} cluster(s) at status=review "
                      f"({len(applied['unchanged'])} already pending, {len(applied['skipped_rejected'])} "
                      "previously rejected). No fact was changed."]
    else:
        lines += ["", "Report only: nothing was written. `--apply` proposes each cluster at status=review; "
                      "facts are never changed or deleted."]
    return "\n".join(lines)
