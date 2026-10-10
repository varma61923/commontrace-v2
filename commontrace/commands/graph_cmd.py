"""`commontrace graph`: manage temporal knowledge graph and entity relationships."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import graph, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "graph",
        help="Manage temporal knowledge graph (nodes, relationships, multi-hop queries, visualization).",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_node = sub.add_parser("node", help="Add or update an entity node.")
    p_node.add_argument("id", help="Node identifier (e.g. service:stripe, tool:git).")
    p_node.add_argument("--type", default="concept",
                        help="Entity type (built-in: " + ", ".join(graph.ENTITY_TYPES)
                        + "; or one the ontology declares).")
    p_node.add_argument("--name", default="", help="Human-readable name.")
    p_node.add_argument("--dest", default=None)
    p_node.set_defaults(func=run_node)

    p_edge = sub.add_parser("edge", help="Add a relationship edge between two nodes.")
    p_edge.add_argument("source", help="Source node ID.")
    p_edge.add_argument("relation", help="Relationship type (built-in: " + ", ".join(graph.RELATIONS)
                        + "; or one the ontology declares, or a declared inverse).")
    p_edge.add_argument("target", help="Target node ID.")
    p_edge.add_argument("--weight", type=float, default=1.0, help="Edge weight/strength (0.0 to 1.0).")
    p_edge.add_argument("--valid-at", default=None, help="When the fact became true (valid time, ISO).")
    p_edge.add_argument("--invalid-at", default=None, help="When the fact stopped being true (valid time, ISO).")
    p_edge.add_argument("--expired-at", default=None, help="When the record stops counting at all (ISO).")
    p_edge.add_argument("--dest", default=None)
    p_edge.set_defaults(func=run_edge)

    p_q = sub.add_parser("query", help="Explore neighbors and multi-hop paths around an entity.")
    p_q.add_argument("entity", help="Entity ID or search text.")
    p_q.add_argument("--hops", type=int, default=1, choices=(1, 2, 3), help="Max hops to traverse.")
    p_q.add_argument("--as-of", default="", help="What was true at this moment (valid time).")
    p_q.add_argument("--known-at", default="", help="As the store knew it at this moment (record time).")
    p_q.add_argument("--dest", default=None)
    p_q.set_defaults(func=run_query)

    p_t = sub.add_parser("timeline", help="How an entity's relations changed: what began, ended, and why.")
    p_t.add_argument("entity", help="Entity ID.")
    p_t.add_argument("--json", action="store_true")
    p_t.add_argument("--dest", default=None)
    p_t.set_defaults(func=run_timeline)

    p_b = sub.add_parser("between", help="Edges valid at some moment in an interval.")
    p_b.add_argument("--start", default=None, help="Interval start (open if omitted).")
    p_b.add_argument("--end", default=None, help="Interval end (open if omitted).")
    p_b.add_argument("--relation", default=None)
    p_b.add_argument("--entity", default=None, help="Only edges touching this entity.")
    p_b.add_argument("--json", action="store_true")
    p_b.add_argument("--dest", default=None)
    p_b.set_defaults(func=run_between)

    p_o = sub.add_parser("ontology", help="Show, create or check the store's ontology.")
    p_o.add_argument("action", choices=("show", "init", "check"))
    p_o.add_argument("--dest", default=None)
    p_o.set_defaults(func=run_ontology)

    p_rnd = sub.add_parser("render", help="Render knowledge graph as Mermaid diagram or JSON.")
    p_rnd.add_argument("--format", default="mermaid", choices=("mermaid", "json"))
    p_rnd.add_argument("--as-of", default="", help="Point-in-time date.")
    p_rnd.add_argument("--dest", default=None)
    p_rnd.set_defaults(func=run_render)

    p_f = sub.add_parser("forget", help="Mark a node as forgotten (temporal decay) or undo forgetting.")
    p_f.add_argument("id", help="Node identifier to forget or restore.")
    p_f.add_argument("--reason", default="", help="Reason for forgetting.")
    p_f.add_argument("--undo", action="store_true", help="Undo previous forgetting.")
    p_f.add_argument("--dest", default=None)
    p_f.set_defaults(func=run_forget)

    p_v = sub.add_parser("version", help="Show the version chain of an entity.")
    p_v.add_argument("id", help="Entity ID to inspect.")
    p_v.add_argument("--dest", default=None)
    p_v.set_defaults(func=run_version)

    p_r = sub.add_parser("revise", help="Record a new version of an entity (the old one stays in its history).")
    p_r.add_argument("id", help="Entity ID to revise.")
    p_r.add_argument("--name", default="", help="New display name.")
    p_r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="Property to set on the new version (repeatable).")
    p_r.add_argument("--dest", default=None)
    p_r.set_defaults(func=run_revise)

    p_p = sub.add_parser("provenance", help="Show where a node or edge came from (source and run).")
    p_p.add_argument("target", help="Node id, or an edge as 'source->target:relation'.")
    p_p.add_argument("--dest", default=None)
    p_p.set_defaults(func=run_provenance)

    p_x = sub.add_parser("extract", help="List the entities a text mentions (no writes).")
    p_x.add_argument("text", help="Text to read; '-' reads stdin.")
    p_x.add_argument("--spacy", choices=("auto", "on", "off"), default="auto")
    p_x.add_argument("--json", action="store_true")
    p_x.add_argument("--dest", default=None)
    p_x.set_defaults(func=run_extract)

    p_l = sub.add_parser("link", help="Extract entities from lessons and link each lesson to them.")
    p_l.add_argument("--lesson", action="append", default=[], help="Only these lesson slugs (repeatable).")
    p_l.add_argument("--spacy", choices=("auto", "on", "off"), default="auto")
    p_l.add_argument("--min-mentions", type=int, default=1, help="Skip entities named by fewer lessons.")
    p_l.add_argument("--json", action="store_true")
    p_l.add_argument("--dest", default=None)
    p_l.set_defaults(func=run_link)

    p_e = sub.add_parser("entities", help="Known entities, most mentioned first.")
    p_e.add_argument("--type", default=None)
    p_e.add_argument("--limit", type=int, default=50)
    p_e.add_argument("--json", action="store_true")
    p_e.add_argument("--dest", default=None)
    p_e.set_defaults(func=run_entities)

    p_d = sub.add_parser("duplicates", help="Entity pairs that look like one thing (candidates for merge).")
    p_d.add_argument("--json", action="store_true")
    p_d.add_argument("--dest", default=None)
    p_d.set_defaults(func=run_duplicates)

    p_m = sub.add_parser("merge", help="Fold a duplicate entity into another; history is kept.")
    p_m.add_argument("keep")
    p_m.add_argument("duplicate")
    p_m.add_argument("--dest", default=None)
    p_m.set_defaults(func=run_merge)

    p_rx = sub.add_parser("extract-relations",
                          help="Extract ontology relations (triples) from a file or text into graph edges.")
    p_rx.add_argument("source", help="A text file, or the text itself; '-' reads stdin.")
    p_rx.add_argument("--llm", action="store_true",
                      help="Extract with the configured LLM (COMMONTRACE_LLM_*) instead of offline patterns.")
    p_rx.add_argument("--dry-run", action="store_true", help="Show the triples without writing them.")
    p_rx.add_argument("--json", action="store_true")
    p_rx.add_argument("--dest", default=None)
    p_rx.set_defaults(func=run_extract_relations)

    p_rs = sub.add_parser("resolve", help="Propose (and with --apply, merge) entities that name one thing.")
    p_rs.add_argument("--embedder", default=None,
                      help="Embedding provider tag (e.g. arctic-m, openai:text-embedding-3-small); default "
                           "COMMONTRACE_GRAPH_EMBEDDER, else character n-gram similarity.")
    p_rs.add_argument("--threshold", type=float, default=None,
                      help="Report pairs at or above this similarity (default 0.85 embeddings, 0.6 n-grams).")
    p_rs.add_argument("--apply", action="store_true",
                      help="Merge compatible pairs at or above the stricter --apply-threshold.")
    p_rs.add_argument("--apply-threshold", type=float, default=None,
                      help="Similarity a pair needs to be merged (default 0.95 embeddings, 0.85 n-grams).")
    p_rs.add_argument("--context", action="store_true", help="Embed each entity with its neighbours' names.")
    p_rs.add_argument("--limit", type=int, default=100)
    p_rs.add_argument("--json", action="store_true")
    p_rs.add_argument("--dest", default=None)
    p_rs.set_defaults(func=run_resolve)


def run_node(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        node = graph.add_node(
            root=root,
            id=args.id,
            entity_type=args.type,
            name=args.name,
        )
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"Saved node '{node.id}' (type: {node.entity_type}, name: '{node.name}').")
    return 0


def run_edge(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        edge = graph.add_edge(
            root=root,
            source=args.source,
            target=args.target,
            relation=args.relation,
            weight=args.weight,
            valid_at=args.valid_at,
            invalid_at=args.invalid_at,
            expired_at=args.expired_at,
        )
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"Saved edge: ({edge.source}) --[{edge.relation}, w={edge.weight}]--> ({edge.target})")
    return 0


def run_query(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        nodes = graph.load_nodes(root)
        entity_id = args.entity.strip().lower()
        start_ids = [entity_id] if entity_id in nodes else graph.extract_entities_from_text(root, args.entity)

        if not start_ids:
            print(f"No known graph entities found matching '{args.entity}'.")
            return 0

        if args.hops == 1:
            for sid in start_ids:
                neighbors = graph.get_neighbors(root, sid, as_of=args.as_of or None,
                                                known_at=args.known_at or None)
                print(f"# Neighbors of '{sid}':")
                if not neighbors:
                    print("  (no connected edges)")
                for n in neighbors:
                    arrow = f"--[{n['relation']}]-->" if n["direction"] == "out" else f"<--[{n['relation']}]--"
                    print(f"  ({sid}) {arrow} ({n['neighbor_id']} / {n['neighbor_name']})")
        else:
            sub = graph.multi_hop_subgraph(root, start_ids, max_hops=args.hops, as_of=args.as_of or None,
                                           known_at=args.known_at or None)
            print(f"# Subgraph: {len(sub['nodes'])} nodes, {len(sub['edges'])} edges")
            print("\n## Nodes:")
            for n in sub["nodes"]:
                hop = sub["hop_distances"].get(n["id"], 0)
                print(f"  - [{n['entity_type']}] {n['id']} ('{n['name']}') [hop={hop}]")
            print("\n## Edges:")
            for e in sub["edges"]:
                print(f"  - ({e['source']}) --[{e['relation']}]--> ({e['target']})")
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    return 0


def run_render(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        if args.format == "json":
            data = graph.export_json(root, as_of=args.as_of or None)
            print(json.dumps(data, indent=2))
        else:
            print(graph.export_mermaid(root, as_of=args.as_of or None))
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    return 0


def run_forget(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        updated = graph.forget_node(root, args.id, reason=args.reason or "forgotten", undo=args.undo)
    except (OSError, ValueError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if updated is None:
        print(f"[commontrace] no graph node '{args.id}'.", file=sys.stderr)
        return 1
    print(f"{'Restored' if args.undo else 'Forgot'} node '{updated.id}'.")
    return 0


def run_version(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    chain = graph.get_version_chain(root, args.id)
    if not chain:
        print(f"[commontrace] no graph node '{args.id}'.", file=sys.stderr)
        return 1
    print(f"# Version chain for '{args.id}': {len(chain)} version(s)")
    for node in chain:
        marks = [m for m, on in (("latest", node.is_latest), ("forgotten", node.is_forgotten)) if on]
        suffix = f" [{', '.join(marks)}]" if marks else ""
        print(f"  v{node.version}  {node.id}  '{node.name}'  updated {node.updated_at[:19]}{suffix}")
    return 0


def run_revise(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    props: dict[str, str] = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            print(f"[commontrace] --set expects KEY=VALUE, got {item!r}.", file=sys.stderr)
            return 2
        props[key.strip()] = value
    try:
        node = graph.create_memory_version(root, args.id, new_properties=props, new_name=args.name or None)
    except (OSError, ValueError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    print(f"Recorded version {node.version} of '{node.root_id}' as '{node.id}'.")
    return 0


def run_provenance(args: argparse.Namespace) -> int:
    from commontrace import provenance

    root = paths.resolve_root(args.dest)
    records = provenance.list_provenance(root, args.target)
    if not records:
        print(f"No provenance recorded for '{args.target}'.")
        return 0
    for rec in records:
        detail = rec.get("detail")
        detail_text = json.dumps(detail, sort_keys=True) if isinstance(detail, (dict, list)) else str(detail or "")
        print(f"{rec.get('created_at', '')[:19]}  {rec.get('target_kind', '')}  "
              f"source={rec.get('source_path') or '-'}  run={rec.get('run_id') or '-'}  {detail_text}")
    return 0


def run_timeline(args: argparse.Namespace) -> int:
    events = graph.timeline(paths.resolve_root(args.dest), args.entity)
    if args.json:
        print(json.dumps(events, indent=2))
        return 0
    if not events:
        print(f"No relations recorded for '{args.entity}'.")
        return 0
    for e in events:
        why = f"  ({e['reason']})" if e.get("reason") else ""
        print(f"{e['at']}  {e['event']:6s} ({e['source']}) --[{e['relation']}]--> ({e['target']}){why}")
    return 0


def run_between(args: argparse.Namespace) -> int:
    try:
        edges = graph.edges_between(paths.resolve_root(args.dest), args.start, args.end,
                                    relation=args.relation, entity=args.entity)
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([e.to_dict() for e in edges], indent=2))
        return 0
    for e in edges:
        print(f"{e.valid_at} .. {e.invalid_at or 'now'}  ({e.source}) --[{e.relation}]--> ({e.target})")
    print(f"[commontrace] {len(edges)} edge(s).", file=sys.stderr)
    return 0


def run_ontology(args: argparse.Namespace) -> int:
    import os

    from commontrace import ontology

    root = paths.resolve_root(args.dest)
    if args.action == "init":
        existing = ontology.ontology_path(root)
        if existing:
            print(f"[commontrace] an ontology already exists: {existing}", file=sys.stderr)
            return 1
        path = os.path.join(paths.memory_dir(root), "ontology.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ontology.TEMPLATE)
        print(f"[commontrace] wrote {path}")
        return 0
    try:
        onto = ontology.load(root)
    except (ontology.OntologyError, ValueError, OSError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.action == "show":
        print(json.dumps(onto.to_dict(), indent=2))
        return 0
    problems = []
    nodes = graph.load_nodes(root)
    for edge in graph.load_edges(root):
        if edge.invalid_at:
            continue
        rel = onto.relations.get(edge.relation)
        if rel is None:
            problems.append(f"{edge.source} -[{edge.relation}]-> {edge.target}: relation not in the ontology")
            continue
        src, dst = nodes.get(edge.source), nodes.get(edge.target)
        if src and dst:
            for p in ontology.Ontology(onto.entity_types, onto.relations).check_edge(
                    rel, src.entity_type, dst.entity_type):
                problems.append(f"{edge.source} -[{edge.relation}]-> {edge.target}: {p}")
    for node in nodes.values():
        if node.entity_type not in onto.entity_types:
            problems.append(f"node {node.id}: type {node.entity_type} not in the ontology")
    for p in problems:
        print(p)
    print(f"[commontrace] {onto.source}: {len(problems)} problem(s) in the current graph.", file=sys.stderr)
    return 1 if problems else 0


def _spacy_flag(value: str):
    return {"auto": None, "on": True, "off": False}[value]


def run_extract(args: argparse.Namespace) -> int:
    from commontrace import entities, ontology

    text = sys.stdin.read() if args.text == "-" else args.text
    try:
        found = entities.extract(text, onto=ontology.load(paths.resolve_root(args.dest)),
                                 use_spacy=_spacy_flag(args.spacy))
    except RuntimeError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([m.to_dict() for m in found], indent=2))
        return 0
    for m in found:
        print(f"{m.key:40s} {m.name!r}  ({m.source})")
    return 0


def run_link(args: argparse.Namespace) -> int:
    from commontrace import entities

    try:
        out = entities.link_lessons(paths.resolve_root(args.dest), args.lesson or None,
                                    use_spacy=_spacy_flag(args.spacy), min_mentions=args.min_mentions)
    except RuntimeError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"[commontrace] linked {out['lessons']} lesson(s) to {out['entities']} entit(ies): "
              f"{out['new_entities']} new, {out['new_links']} new link(s), {out['retired_links']} retired "
              f"({out['backend']}).")
    return 0


def run_entities(args: argparse.Namespace) -> int:
    from commontrace import entities

    rows = entities.entities(paths.resolve_root(args.dest), type_=args.type, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        also = f"  aka {', '.join(a for a in r['aliases'] if a != r['name'])}" if len(r["aliases"]) > 1 else ""
        print(f"{r['mentions']:4d}  {r['id']}{also}")
    return 0


def run_duplicates(args: argparse.Namespace) -> int:
    from commontrace import entities

    pairs = entities.duplicates(paths.resolve_root(args.dest))
    if args.json:
        print(json.dumps(pairs, indent=2))
        return 0
    for p in pairs:
        print(f"{p['a']}  ~  {p['b']}   ({p['because']})")
    print(f"[commontrace] {len(pairs)} candidate pair(s); fold one into another with `graph merge KEEP DUP`.",
          file=sys.stderr)
    return 0


def run_merge(args: argparse.Namespace) -> int:
    from commontrace import entities

    try:
        out = entities.merge(paths.resolve_root(args.dest), args.keep, args.duplicate)
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    print(f"[commontrace] merged {out['merged']} into {out['kept']} ({out['edges_moved']} edge(s) moved).")
    return 0


def _relation_source(source: str) -> tuple[str, str]:
    """(label, text) for a file path, '-' (stdin) or literal text."""
    import os

    from commontrace import relation_extraction

    if source == "-":
        return "<stdin>", sys.stdin.read(relation_extraction.MAX_TEXT + 1)
    if os.path.isfile(source):
        with open(source, encoding="utf-8", errors="replace") as fh:
            return os.path.abspath(source), fh.read(relation_extraction.MAX_TEXT + 1)
    return "<text>", source


def run_extract_relations(args: argparse.Namespace) -> int:
    from commontrace import llm, relation_extraction

    try:
        label, text = _relation_source(args.source)
        report = relation_extraction.ingest_text(paths.resolve_root(args.dest), text, source=label,
                                                 llm=llm.complete if args.llm else None, dry_run=args.dry_run)
    except (OSError, ValueError, llm.LLMUnavailable) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for t in report["extracted"]:
        when = f"  valid_at={t['valid_at']}" if t["valid_at"] else ""
        print(f"({t['subject']}) --[{t['relation']}]--> ({t['object']})  conf={t['confidence']:.2f}{when}")
    for error in report["errors"]:
        print(f"  - {error}", file=sys.stderr)
    verb = "would write" if args.dry_run else "wrote"
    edges = report["triples"] if args.dry_run else report["edges_written"]
    print(f"[commontrace] {report['triples']} triple(s); {verb} {edges} edge(s) from {label}.", file=sys.stderr)
    return 1 if report["errors"] else 0


def run_resolve(args: argparse.Namespace) -> int:
    from commontrace import embeddings, entity_resolution

    try:
        out = entity_resolution.resolve(paths.resolve_root(args.dest), embedder=args.embedder,
                                        threshold=args.threshold, apply=args.apply,
                                        apply_threshold=args.apply_threshold, context=args.context,
                                        limit=args.limit)
    except (ValueError, embeddings.EmbeddingError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    for p in out["pairs"]:
        flag = "" if p["compatible"] else "  [blocked: incompatible types]"
        print(f"{p['score']:.3f}  {p['a']}  ~  {p['b']}{flag}")
    for m in out["merged"]:
        print(f"merged {m['merged']} into {m['kept']} ({m['method']} {m['score']:.3f})")
    for s in out["skipped"]:
        print(f"skipped {s['a']} ~ {s['b']}: {s['reason']}")
    print(f"[commontrace] {len(out['pairs'])} candidate pair(s) by {out['method']} >= {out['threshold']}; "
          f"{len(out['merged'])} merged (>= {out['apply_threshold']}).", file=sys.stderr)
    return 0
