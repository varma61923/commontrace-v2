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
    p_node.add_argument("--type", default="concept", choices=graph.ENTITY_TYPES, help="Entity type.")
    p_node.add_argument("--name", default="", help="Human-readable name.")
    p_node.add_argument("--dest", default=None)
    p_node.set_defaults(func=run_node)

    p_edge = sub.add_parser("edge", help="Add a relationship edge between two nodes.")
    p_edge.add_argument("source", help="Source node ID.")
    p_edge.add_argument("relation", choices=graph.RELATIONS, help="Relationship type.")
    p_edge.add_argument("target", help="Target node ID.")
    p_edge.add_argument("--weight", type=float, default=1.0, help="Edge weight/strength (0.0 to 1.0).")
    p_edge.add_argument("--valid-at", default=None, help="When fact becomes true (transaction time, ISO format).")
    p_edge.add_argument("--invalid-at", default=None, help="When fact becomes false (transaction time, ISO format).")
    p_edge.add_argument("--expired-at", default=None, help="When record expires (validity time, ISO format).")
    p_edge.add_argument("--dest", default=None)
    p_edge.set_defaults(func=run_edge)

    p_q = sub.add_parser("query", help="Explore neighbors and multi-hop paths around an entity.")
    p_q.add_argument("entity", help="Entity ID or search text.")
    p_q.add_argument("--hops", type=int, default=1, choices=(1, 2, 3), help="Max hops to traverse.")
    p_q.add_argument("--as-of", default="", help="Point-in-time date.")
    p_q.add_argument("--dest", default=None)
    p_q.set_defaults(func=run_query)

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
                neighbors = graph.get_neighbors(root, sid, as_of=args.as_of or None)
                print(f"# Neighbors of '{sid}':")
                if not neighbors:
                    print("  (no connected edges)")
                for n in neighbors:
                    arrow = f"--[{n['relation']}]-->" if n["direction"] == "out" else f"<--[{n['relation']}]--"
                    print(f"  ({sid}) {arrow} ({n['neighbor_id']} / {n['neighbor_name']})")
        else:
            sub = graph.multi_hop_subgraph(root, start_ids, max_hops=args.hops, as_of=args.as_of or None)
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
