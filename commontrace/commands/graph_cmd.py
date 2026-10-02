"""`commontrace graph`: manage temporal knowledge graph and entity relationships (Zep + Cognee pattern)."""
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

    # node
    p_node = sub.add_parser("node", help="Add or update an entity node.")
    p_node.add_argument("id", help="Node identifier (e.g. service:stripe, tool:git).")
    p_node.add_argument("--type", default="concept", choices=graph.ENTITY_TYPES, help="Entity type.")
    p_node.add_argument("--name", default="", help="Human-readable name.")
    p_node.add_argument("--dest", default=None)
    p_node.set_defaults(func=run_node)

    # edge
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

    # query
    p_q = sub.add_parser("query", help="Explore neighbors and multi-hop paths around an entity.")
    p_q.add_argument("entity", help="Entity ID or search text.")
    p_q.add_argument("--hops", type=int, default=1, choices=(1, 2, 3), help="Max hops to traverse.")
    p_q.add_argument("--as-of", default="", help="Point-in-time date.")
    p_q.add_argument("--dest", default=None)
    p_q.set_defaults(func=run_query)

    # render
    p_rnd = sub.add_parser("render", help="Render knowledge graph as Mermaid diagram or JSON.")
    p_rnd.add_argument("--format", default="mermaid", choices=("mermaid", "json"))
    p_rnd.add_argument("--as-of", default="", help="Point-in-time date.")
    p_rnd.add_argument("--dest", default=None)
    p_rnd.set_defaults(func=run_render)

    # forget
    p_f = sub.add_parser("forget", help="Mark a node as forgotten (temporal decay) or undo forgetting.")
    p_f.add_argument("id", help="Node identifier to forget or restore.")
    p_f.add_argument("--reason", default="", help="Reason for forgetting.")
    p_f.add_argument("--undo", action="store_true", help="Undo previous forgetting.")
    p_f.add_argument("--dest", default=None)
    p_f.set_defaults(func=run_forget)

    # version
    p_v = sub.add_parser("version", help="List version chains / memory evolution for an entity.")
    p_v.add_argument("id", help="Entity ID to inspect.")
    p_v.add_argument("--dest", default=None)
    p_v.set_defaults(func=run_version)


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
        updated = graph.forget_node(root, args.id, reason=args.reason, undo=args.undo)
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    action = "Restored" if args.undo else "Forgot"
    print(f"{action} node '{updated.id}'.")
    return 0


def run_version(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        chains = graph.list_version_chains(root, args.id)
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"# Version chains for '{args.id}': {len(chains)} found")
    for chain in chains:
        print(f"  - [{chain.relationship}] {chain.source} -> {chain.target} (created: {chain.created_at})")
    return 0
