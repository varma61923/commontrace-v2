"""Topic communities over lesson and fact nodes, built by label propagation.

Literature grounding (cited, not vendored):

- Community detection is label propagation per Raghavan, Albert & Kumara
  2007 ("Near linear time community detection on large-scale networks"):
  every node starts with a unique label, each node then adopts the plurality
  label among its neighbours, sweeps repeat until a full sweep converges or
  100 sweeps pass. Each sweep is O(m) over m edges. Stdlib only: unique-label
  init, plurality adoption, a seeded RNG for the visitation order, and sorted
  tie-breaks, so the same store content always yields the same communities.
- Summaries are extractive -- top terms plus member titles -- and never call
  an LLM, so a build is deterministic and offline.

Graph: nodes are active lessons (canonical slug) and active facts (fact id).
Edges come from shared signals: a source_trace, a tag, or an entity-store
link (entity_store.rebuild_entity_index maps both kinds of id). Storage:
memory/communities/*.md frontmatter plus a member list, like the rest of
the store, written atomically via commontrace.frontmatter.
"""
from __future__ import annotations

import collections
import contextlib
import glob
import os
import random
import re
from datetime import datetime, timezone
from typing import Any

from commontrace import entity_store, frontmatter, hierarchical, lesson_io, paths

MAX_SWEEPS = 100
SEED = 0

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were by at from as that this it its be not no yes "
    "we you i they he she when where which who how why if then than so such only also very just use used "
    "when do not does did has have had will would can could should may might into over under again every".split()
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _communities_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "communities")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "community"


def _load_nodes(root: str) -> dict[str, dict[str, Any]]:
    """Active lessons (by canonical slug) and active facts (by id)."""
    nodes: dict[str, dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))):
        if os.path.basename(path) in ("lesson_template.md", "README.md"):
            continue
        try:
            fm, _body = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - an unreadable lesson is skipped, not fatal
            continue
        if (fm.get("status") or "active") != "active":
            continue
        slug = lesson_io.canonical_slug(os.path.basename(path))
        if not slug:
            continue
        description = str(fm.get("description") or fm.get("name") or slug)
        tags = sorted({str(t).strip() for t in (fm.get("tags") or []) if str(t).strip()})
        traces = sorted({str(t).strip() for t in (fm.get("source_traces") or []) if str(t).strip()})
        nodes[slug] = {
            "id": slug,
            "kind": "lesson",
            "title": description,
            "tags": tags,
            "source_traces": traces,
            "text": f"{description} {fm.get('domain') or ''} {' '.join(tags)}",
        }
    now = datetime.now(timezone.utc)
    for fact in sorted(hierarchical.list_facts(root, now=now), key=lambda f: f.id):
        if not hierarchical._valid_at(fact, now):
            continue
        nodes[fact.id] = {
            "id": fact.id,
            "kind": "fact",
            "title": fact.statement,
            "tags": [],
            "source_traces": sorted({str(t) for t in fact.source_traces if str(t).strip()}),
            "text": fact.statement,
        }
    return nodes


def _build_adjacency(root: str, nodes: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Neighbour list per node: shared source_traces, tags, and entity links."""
    by_trace: dict[str, list[str]] = collections.defaultdict(list)
    by_tag: dict[str, list[str]] = collections.defaultdict(list)
    for nid, node in nodes.items():
        for trace_id in node["source_traces"]:
            by_trace[trace_id].append(nid)
        for tag in node["tags"]:
            by_tag[tag].append(nid)
    adj: dict[str, set[str]] = {nid: set() for nid in nodes}
    seen_groups: set[tuple[str, ...]] = set()

    def _link(members: list[str]) -> None:
        # Signals only establish an unweighted edge. Identical membership
        # groups establish exactly the same clique regardless of their label.
        # Keep every distinct group; never truncate a high-degree community.
        unique = tuple(sorted(set(members)))
        if len(unique) < 2 or unique in seen_groups:
            return
        seen_groups.add(unique)
        for i, source in enumerate(unique):
            for target in unique[i + 1:]:
                adj[source].add(target)
                adj[target].add(source)

    for members in by_trace.values():
        _link(members)
    for members in by_tag.values():
        _link(members)
    try:
        entities = entity_store.load_entities(root)
    except OSError:
        entities = {}
    for entry in entities.values():
        members = sorted({str(m) for m in entry.get("memory_ids", []) if str(m) in nodes})
        _link(members)
    return {nid: sorted(neighbours) for nid, neighbours in adj.items()}


def _label_propagation(node_ids: list[str], adj: dict[str, list[str]]) -> dict[str, str]:
    """Raghavan-Albert-Kumara 2007: unique-label init, plurality adoption, seeded order."""
    labels = {nid: nid for nid in node_ids}
    order = sorted(node_ids)
    rng = random.Random(SEED)
    for _sweep in range(MAX_SWEEPS):
        rng.shuffle(order)
        changed = False
        for nid in order:
            neighbours = adj.get(nid) or []
            if not neighbours:
                continue
            counts: collections.Counter = collections.Counter(labels[n] for n in neighbours)
            best = min(counts, key=lambda lab: (-counts[lab], lab))
            if best != labels[nid]:
                labels[nid] = best
                changed = True
        if not changed:
            break
    return labels


def _summary(nodes: dict[str, dict[str, Any]], members: list[str]) -> dict[str, Any]:
    """Extractive summary: top terms across members, plus member titles."""
    terms: list[str] = []
    titles: list[str] = []
    for nid in members:
        node = nodes[nid]
        title = node["title"]
        titles.append(title if len(title) <= 80 else title[:77] + "...")
        terms.extend(
            tok
            for tok in _TOKEN_RE.findall(str(node["text"]).lower())
            if tok not in _STOPWORDS and len(tok) > 2
        )
    counts: collections.Counter = collections.Counter(terms)
    top = [term for term, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    shown = sorted(titles)[:5]
    summary = f"Top terms: {', '.join(top)}. Members: {', '.join(shown)}." if members else ""
    return {"top_terms": top, "member_titles": sorted(titles), "summary": summary}


def build_communities(root: str) -> dict[str, list[str]]:
    """Rebuild memory/communities/ from the current lessons and facts.

    Returns ``{community_name: [member ids]}``. Deterministic: same store
    content, same partition, same files (``updated_at`` aside).
    """
    nodes = _load_nodes(root)
    adj = _build_adjacency(root, nodes)
    labels = _label_propagation(sorted(nodes), adj)
    groups: dict[str, list[str]] = {}
    for nid, label in labels.items():
        groups.setdefault(label, []).append(nid)
    groups = {name: sorted(members) for name, members in sorted(groups.items())}
    directory = _communities_dir(root)
    os.makedirs(directory, exist_ok=True)
    keep: set[str] = set()
    for name, members in groups.items():
        summary = _summary(nodes, members)
        fm = {
            "name": name,
            "size": len(members),
            "members": members,
            "top_terms": summary["top_terms"],
            "member_titles": summary["member_titles"],
            "summary": summary["summary"],
            "updated_at": _now(),
        }
        filename = _slugify(name) + ".md"
        keep.add(filename)
        body = (
            "## Community\n\n"
            + summary["summary"]
            + "\n\n## Members\n\n"
            + "\n".join(f"- {member}" for member in members)
            + "\n"
        )
        frontmatter.write(os.path.join(directory, filename), fm, body)
    for stale in glob.glob(os.path.join(directory, "*.md")):
        if os.path.basename(stale) not in keep:
            with contextlib.suppress(OSError):
                os.unlink(stale)
    return groups


def update_community_for_lesson(root: str, slug: str) -> str | None:
    """Best-effort refresh after a lesson changes; returns its community name.

    Never raises: a community build is a derived artifact and must not break
    the lesson write path that calls it.
    """
    try:
        want = lesson_io.canonical_slug(slug)
        groups = build_communities(root)
        for name, members in groups.items():
            if want in members:
                return name
        return None
    except Exception:  # noqa: BLE001 - best-effort by contract
        return None


def get_community(root: str, name: str) -> dict[str, Any] | None:
    """One stored community's frontmatter by name (exact, then sanitized match)."""
    directory = _communities_dir(root)
    candidate = os.path.join(directory, _slugify(name) + ".md")
    candidates = [candidate]
    for path in sorted(glob.glob(os.path.join(directory, "*.md"))):
        if path not in candidates:
            candidates.append(path)
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            fm, _body = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - skip unreadable files
            continue
        if path == candidate or str(fm.get("name", "")) == str(name):
            return fm
    return None


def list_communities(root: str) -> list[dict[str, Any]]:
    """Every stored community's frontmatter, sorted by name."""
    out: list[dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(_communities_dir(root), "*.md"))):
        try:
            fm, _body = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - skip unreadable files
            continue
        out.append(fm)
    return sorted(out, key=lambda fm: str(fm.get("name", "")))
