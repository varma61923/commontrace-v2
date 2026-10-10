"""`commontrace ontology`: inspect or install the store's knowledge-graph ontology."""
from __future__ import annotations

import argparse
import os
import sys

from commontrace import ontology, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "ontology",
        help="Show the active ontology or install the starter one.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_show = sub.add_parser("show", help="Print the active ontology source and summary.")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=run_show)

    p_set = sub.add_parser(
        "set-starter", help="Write the starter ontology.yaml (refused if one exists).")
    p_set.add_argument("--dest", default=None)
    p_set.add_argument("--force", action="store_true", help="Overwrite an existing ontology file.")
    p_set.set_defaults(func=run_set_starter)
    p_learn = sub.add_parser("propose", help="Learn a review-only ontology from observed graph structure.")
    p_learn.add_argument("--dest", default=None)
    p_learn.set_defaults(func=run_propose)
    p_schema = sub.add_parser("schema", help="Install a prescribed JSON-schema ontology.")
    p_schema.add_argument("schema_file")
    p_schema.add_argument("--replace", action="store_true")
    p_schema.add_argument("--dest", default=None)
    p_schema.set_defaults(func=run_schema)
    p_cls = sub.add_parser("classify", help="Classify an entity mention into the ontology's entity types.")
    p_cls.add_argument("text", help="The mention (with --extract: a text whose mentions are classified); "
                                    "'-' reads stdin.")
    p_cls.add_argument("--context", default="", help="Text the mention appears in.")
    p_cls.add_argument("--extract", action="store_true", help="Classify every entity mentioned in the text.")
    p_cls.add_argument("--embedder", default=None,
                       help="Embedding provider tag (e.g. arctic-m, openai:text-embedding-3-small); "
                            "default COMMONTRACE_GRAPH_EMBEDDER, else none.")
    p_cls.add_argument("--llm", action="store_true", help="Also ask the configured LLM (COMMONTRACE_LLM_*).")
    p_cls.add_argument("--threshold", type=float, default=None, help="Minimum confidence (default 0.6).")
    p_cls.add_argument("--json", action="store_true")
    p_cls.add_argument("--dest", default=None)
    p_cls.set_defaults(func=run_classify)


def run_classify(args: argparse.Namespace) -> int:
    import json

    from commontrace import entities, llm, ontology_classify

    text = sys.stdin.read() if args.text == "-" else args.text
    try:
        onto = ontology.load(paths.resolve_root(args.dest))
        options = {"onto": onto, "embedder": args.embedder, "llm": llm.complete if args.llm else None}
        if args.threshold is not None:
            options["threshold"] = args.threshold
        if args.extract:
            names = [m.name for m in entities.extract(text, use_spacy=False)]
            rows = [{"mention": n, **ontology_classify.classify(n, text, **options).to_dict()} for n in names]
        else:
            found = ontology_classify.classify(text, args.context, **options)
            rows = [{"mention": text.strip(), **found.to_dict()}]
    except (ValueError, RuntimeError, TypeError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        lineage = "  (" + " is a ".join(row["ancestors"]) + ")" if len(row["ancestors"]) > 1 else ""
        print(f"{row['mention']!r}: {row['type'] or 'unknown'}  confidence={row['confidence']:.2f}  "
              f"method={row['method']}{lineage}")
    return 0


def run_propose(args):
    import json

    from commontrace import ontology_learning

    print(json.dumps(ontology_learning.propose(paths.resolve_root(args.dest)), indent=2))
    return 0


def run_schema(args):
    import json

    from commontrace import ontology_learning

    with open(args.schema_file, encoding="utf-8") as fh:
        schema = json.load(fh)
    result = ontology_learning.prescribe(paths.resolve_root(args.dest), schema, replace=args.replace)
    print(json.dumps(result, indent=2))
    return 0


def run_show(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    path = ontology.ontology_path(root)
    onto = ontology.load(root)
    print(f"source: {path or '(built-in defaults)'}")
    print(f"entity types: {len(onto.entity_types)}  relations: {len(onto.relations)}  "
          f"strict: {onto.strict}")
    return 0


def run_set_starter(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    existing = ontology.ontology_path(root)
    if existing is not None and not args.force:
        print(f"[commontrace] ontology already set at {existing} (use --force to overwrite)",
              file=sys.stderr)
        return 1
    out = os.path.join(paths.memory_dir(root), "ontology.yaml")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(ontology.TEMPLATE)
    print(f"Wrote starter ontology to {out}.")
    return 0
