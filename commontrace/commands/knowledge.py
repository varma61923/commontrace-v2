"""Related CLI operations with lazy, operation-local dependencies."""

from __future__ import annotations

import argparse


# kb operations
def kb_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace.commands import _validators

    p = subparsers.add_parser(
        "kb",
        help="Curated substrate lesson packs (webhooks, migrations, deploys, ...) "
        "installed into this store for review.",
    )
    sub = p.add_subparsers(dest="kb_cmd", required=True)

    ls = sub.add_parser("list", help="List the packs that ship with this install.")
    ls.set_defaults(func=kb_run_list)

    inst = sub.add_parser(
        "install",
        help="Install a pack's lessons at status=review (never active).",
    )
    inst.add_argument("pack")
    inst.add_argument("--agent-type", type=_validators.agent_type, default=None)
    inst.add_argument("--dest", default=None)
    inst.set_defaults(func=kb_run_install)


def kb_run_list(args: argparse.Namespace) -> int:
    from commontrace import kb_packs

    for info in kb_packs.list_packs():
        print(f"  {info.name:<24} {info.count:>3} lessons  v{info.version}  {info.description}")
    return 0


def kb_run_install(args: argparse.Namespace) -> int:
    import sys

    from commontrace import kb_packs, paths

    root = paths.resolve_root(args.dest)
    paths.warn_if_implicit_cwd_store(args.dest)
    try:
        result = kb_packs.install_pack(root, args.pack, agent_type=args.agent_type)
    except kb_packs.UnknownPack as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    print(
        f"[commontrace] {result.pack}@{result.version}: {len(result.written)} lesson(s) "
        f"written at status=review, {len(result.skipped_existing)} already installed (left untouched)."
    )
    if result.written:
        print(
            "  Each needs one judgement the pack cannot make -- where it does NOT apply in\n"
            "  your stack (`do_not_apply_when`, `## Counter-examples`) -- before\n"
            "  `commontrace lesson approve` will activate it:\n"
            "    commontrace lesson list --status review"
        )
    return 0


# community operations
def community_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "community",
        help="Topic communities over lessons and facts (label propagation, extractive summaries).",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_build = sub.add_parser("build", help="Rebuild memory/communities/ from current lessons and facts.")
    p_build.add_argument("--json", action="store_true")
    p_build.add_argument("--dest", default=None)
    p_build.set_defaults(func=community_run_build)

    p_list = sub.add_parser("list", help="List the stored communities.")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=community_run_list)

    p_show = sub.add_parser("show", help="Show one community's members and summary.")
    p_show.add_argument("name", help="Community name (from `community list`).")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=community_run_show)


def community_run_build(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import communities, paths

    root = paths.resolve_root(args.dest)
    try:
        groups = communities.build_communities(root)
    except Exception as exc:  # noqa: BLE001
        print(f"[commontrace] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({name: members for name, members in groups.items()}, indent=2))
        return 0
    n_members = sum(len(members) for members in groups.values())
    print(
        f"Built {len(groups)} communit{'y' if len(groups) == 1 else 'ies'} "
        f"over {n_members} node(s) -> memory/communities/"
    )
    for name, members in groups.items():
        print(f"  {name}  ({len(members)} member(s))")
    return 0


def community_run_list(args: argparse.Namespace) -> int:
    import json

    from commontrace import communities, paths

    rows = communities.list_communities(paths.resolve_root(args.dest))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No communities stored. Run `commontrace community build`.")
        return 0
    print(f"{'NAME':<40} {'SIZE':>4}  TOP TERMS")
    print("-" * 80)
    for row in rows:
        terms = ", ".join(str(t) for t in (row.get("top_terms") or [])[:4])
        print(f"{str(row.get('name', '')):<40} {int(row.get('size') or 0):>4}  {terms}")
    return 0


def community_run_show(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import communities, paths

    community = communities.get_community(paths.resolve_root(args.dest), args.name)
    if community is None:
        print(f"[commontrace] no community named {args.name!r}; try `community list`.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(community, indent=2))
        return 0
    print(f"# {community.get('name', args.name)}  ({int(community.get('size') or 0)} member(s))")
    if community.get("summary"):
        print(str(community["summary"]))
    print()
    for member in community.get("members") or []:
        print(f"  - {member}")
    return 0


# observation operations
def observation_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace import observations

    p = subparsers.add_parser(
        "observation",
        help="Evidence-grounded observations consolidated from reinforced facts.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="List stored observations.")
    p_list.add_argument("--trend", default="", choices=("", *observations.TRENDS))
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=observation_run_list)

    p_show = sub.add_parser("show", help="Show one observation with its evidence.")
    p_show.add_argument("id", help="Observation id (obs-...).")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=observation_run_show)

    p_con = sub.add_parser("consolidate", help="Fold reinforced facts (confirmations>=2) into observations.")
    p_con.add_argument("--json", action="store_true")
    p_con.add_argument("--dest", default=None)
    p_con.set_defaults(func=observation_run_consolidate)


def observation_run_list(args: argparse.Namespace) -> int:
    import json

    from commontrace import observations, paths

    rows = observations.load_observations(paths.resolve_root(args.dest))
    ordered = [rows[key] for key in sorted(rows)]
    if args.trend:
        ordered = [o for o in ordered if o.trend == args.trend]
    if args.json:
        print(json.dumps([o.to_dict() for o in ordered], indent=2))
        return 0
    if not ordered:
        print("No observations stored. Run `commontrace observation consolidate`.")
        return 0
    print(f"{'ID':<18} {'TREND':<14} {'PROOFS':>6}  STATEMENT")
    print("-" * 80)
    for o in ordered:
        statement = o.statement if len(o.statement) <= 45 else o.statement[:42] + "..."
        print(f"{o.id:<18} {o.trend:<14} {o.proof_count:>6}  {statement}")
    return 0


def observation_run_show(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import observations, paths

    observation = observations.get_observation(paths.resolve_root(args.dest), args.id)
    if observation is None:
        print(f"[commontrace] no observation {args.id!r}; try `observation list`.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(observation.to_dict(), indent=2))
        return 0
    print(
        f"# {observation.id}  (trend: {observation.trend}, proofs: {observation.proof_count},"
        f" boost: {observations.observation_boost(observation.proof_count)})"
    )
    print(observation.statement)
    print()
    print("## Evidence")
    for entry in observation.evidence:
        print(
            f"- [{entry.get('source_id') or '-'}] {entry.get('quote', '')}"
            f"{('  (' + str(entry['at']) + ')') if entry.get('at') else ''}"
        )
    return 0


def observation_run_consolidate(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import observations, paths

    try:
        results = observations.consolidate_facts(paths.resolve_root(args.dest))
    except Exception as exc:  # noqa: BLE001
        print(f"[commontrace] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([o.to_dict() for o in results], indent=2))
        return 0
    print(f"Consolidated {len(results)} observation(s) from reinforced facts (confirmations>=2).")
    for o in results:
        print(f"  {o.id}  [{o.trend}]  proofs={o.proof_count}  {o.statement[:60]}")
    return 0


# procedural operations
def procedural_add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "procedural",
        help="Record, list, and replay structured agent procedural workflows.",
        description="Manage procedural memory trajectories with token-budgeted replay.",
    )
    sub = parser.add_subparsers(dest="procedural_op", required=True)

    # list
    p_list = sub.add_parser("list", help="List procedural memories in store.")
    p_list.add_argument("--root", default=".", help="Store root directory.")
    p_list.add_argument("--agent-id", default=None, help="Filter by agent ID.")

    # replay
    p_replay = sub.add_parser("replay", help="Replay procedural memory into structured prompt context.")
    p_replay.add_argument("memory_id", help="Procedural memory ID.")
    p_replay.add_argument("--root", default=".", help="Store root directory.")
    p_replay.add_argument("--budget", type=int, default=1500, help="Token budget constraint.")

    # create
    p_create = sub.add_parser("create", help="Create procedural memory from JSON input.")
    p_create.add_argument("--root", default=".", help="Store root directory.")
    p_create.add_argument("--objective", required=True, help="Task objective.")
    p_create.add_argument("--status", default="in_progress", help="Progress status.")
    p_create.add_argument("--agent-id", default="", help="Agent ID.")
    p_create.add_argument("--steps-json", help="JSON string or file containing array of steps.")

    parser.set_defaults(func=procedural_run)


def procedural_run(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import procedural

    op = args.procedural_op
    root = args.root

    if op == "list":
        items = procedural.list_procedural_memories(root, agent_id=args.agent_id)
        if not items:
            print("No procedural memories recorded.")
            return 0
        for item in items:
            print(
                f"[{item['id']}] {item['task_objective']} - {item['progress_status']} "
                f"({item['steps_count']} steps, ~{item['token_estimate']} tok)"
            )
        return 0

    if op == "replay":
        mem = procedural.load_procedural_memory(root, args.memory_id)
        if mem is None:
            print(f"Error: Procedural memory '{args.memory_id}' not found.", file=sys.stderr)
            return 1
        rendered = procedural.format_procedural_memory(mem, token_budget=args.budget)
        print(rendered)
        return 0

    if op == "create":
        steps_raw = []
        if args.steps_json:
            try:
                steps_raw = json.loads(args.steps_json)
            except Exception:
                with open(args.steps_json, encoding="utf-8") as f:
                    steps_raw = json.load(f)
        import uuid

        mem = procedural.ProceduralMemory(
            id=uuid.uuid4().hex[:16],
            task_objective=args.objective,
            progress_status=args.status,
            steps=[procedural.ProceduralStep.from_dict(s) for s in steps_raw],
            agent_id=args.agent_id,
        )
        saved_path = procedural.save_procedural_memory(root, mem)
        print(f"Created procedural memory {mem.id} at {saved_path}")
        return 0

    return 0


# ontology operations
def ontology_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "ontology",
        help="Show the active ontology or install the starter one.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_show = sub.add_parser("show", help="Print the active ontology source and summary.")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=ontology_run_show)

    p_set = sub.add_parser("set-starter", help="Write the starter ontology.yaml (refused if one exists).")
    p_set.add_argument("--dest", default=None)
    p_set.add_argument("--force", action="store_true", help="Overwrite an existing ontology file.")
    p_set.set_defaults(func=ontology_run_set_starter)
    p_learn = sub.add_parser("propose", help="Learn a review-only ontology from observed graph structure.")
    p_learn.add_argument("--dest", default=None)
    p_learn.set_defaults(func=ontology_run_propose)
    p_schema = sub.add_parser("schema", help="Install a prescribed JSON-schema ontology.")
    p_schema.add_argument("schema_file")
    p_schema.add_argument("--replace", action="store_true")
    p_schema.add_argument("--dest", default=None)
    p_schema.set_defaults(func=ontology_run_schema)
    p_cls = sub.add_parser("classify", help="Classify an entity mention into the ontology's entity types.")
    p_cls.add_argument(
        "text", help="The mention (with --extract: a text whose mentions are classified); '-' reads stdin."
    )
    p_cls.add_argument("--context", default="", help="Text the mention appears in.")
    p_cls.add_argument("--extract", action="store_true", help="Classify every entity mentioned in the text.")
    p_cls.add_argument(
        "--embedder",
        default=None,
        help="Embedding provider tag (e.g. arctic-m, openai:text-embedding-3-small); "
        "default COMMONTRACE_GRAPH_EMBEDDER, else none.",
    )
    p_cls.add_argument("--llm", action="store_true", help="Also ask the configured LLM (COMMONTRACE_LLM_*).")
    p_cls.add_argument("--threshold", type=float, default=None, help="Minimum confidence (default 0.6).")
    p_cls.add_argument("--json", action="store_true")
    p_cls.add_argument("--dest", default=None)
    p_cls.set_defaults(func=ontology_run_classify)


def ontology_run_classify(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import entities, llm, ontology, ontology_classify, paths

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
        print(
            f"{row['mention']!r}: {row['type'] or 'unknown'}  confidence={row['confidence']:.2f}  "
            f"method={row['method']}{lineage}"
        )
    return 0


def ontology_run_propose(args):
    import json

    from commontrace import ontology_learning, paths

    print(json.dumps(ontology_learning.propose(paths.resolve_root(args.dest)), indent=2))
    return 0


def ontology_run_schema(args):
    import json

    from commontrace import ontology_learning, paths

    with open(args.schema_file, encoding="utf-8") as fh:
        schema = json.load(fh)
    result = ontology_learning.prescribe(paths.resolve_root(args.dest), schema, replace=args.replace)
    print(json.dumps(result, indent=2))
    return 0


def ontology_run_show(args: argparse.Namespace) -> int:
    from commontrace import ontology, paths

    root = paths.resolve_root(args.dest)
    path = ontology.ontology_path(root)
    onto = ontology.load(root)
    print(f"source: {path or '(built-in defaults)'}")
    print(f"entity types: {len(onto.entity_types)}  relations: {len(onto.relations)}  strict: {onto.strict}")
    return 0


def ontology_run_set_starter(args: argparse.Namespace) -> int:
    import os
    import sys

    from commontrace import ontology, paths

    root = paths.resolve_root(args.dest)
    existing = ontology.ontology_path(root)
    if existing is not None and not args.force:
        print(f"[commontrace] ontology already set at {existing} (use --force to overwrite)", file=sys.stderr)
        return 1
    out = os.path.join(paths.memory_dir(root), "ontology.yaml")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(ontology.TEMPLATE)
    print(f"Wrote starter ontology to {out}.")
    return 0


# page operations
def page_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "page",
        help="Manage curated knowledge pages / mental models with dry-run diffs.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)
    tree = sub.add_parser("tree", help="Browse the wiki hierarchy.")
    tree.add_argument("--dest")
    tree.set_defaults(func=page_run_tree)
    watch = sub.add_parser("watch", help="Refresh a page in the background when source facts change.")
    watch.add_argument("slug")
    watch.add_argument("--question", required=True)
    watch.add_argument("--source", action="append", required=True)
    watch.add_argument("--context", action="append", required=True)
    watch.add_argument("--dest")
    watch.set_defaults(func=page_run_watch)

    # list
    p_list = sub.add_parser("list", help="List all curated knowledge pages.")
    p_list.add_argument("--tag", default="", help="Filter by tag.")
    p_list.add_argument("--json", action="store_true", help="Output JSON.")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=page_run_list)

    # get
    p_get = sub.add_parser("get", help="Get a knowledge page by slug.")
    p_get.add_argument("slug", help="Slug of the page (e.g. architecture/auth).")
    p_get.add_argument("--version", type=int, default=None, help="Specific version to fetch.")
    p_get.add_argument("--json", action="store_true", help="Output JSON.")
    p_get.add_argument("--dest", default=None)
    p_get.set_defaults(func=page_run_get)

    # update / set
    p_set = sub.add_parser("set", help="Create or update a curated knowledge page.")
    p_set.add_argument("slug", help="Slug of the page.")
    p_set.add_argument("content", help="Markdown content.")
    p_set.add_argument("--title", default="", help="Title of the page.")
    p_set.add_argument("--tags", nargs="*", default=None, help="Tags for this page.")
    p_set.add_argument("--expected-version", type=int, default=None, help="Optimistic concurrency version.")
    p_set.add_argument("--dry-run", action="store_true", help="Preview line-by-line diff with zero disk writes.")
    p_set.add_argument("--comment", default="", help="Commit rationale / comment.")
    p_set.add_argument("--dest", default=None)
    p_set.set_defaults(func=page_run_set)

    # diff
    p_diff = sub.add_parser("diff", help="Generate dry-run diff against an existing page.")
    p_diff.add_argument("slug", help="Slug of the page.")
    p_diff.add_argument("content", help="Proposed new content.")
    p_diff.add_argument("--dest", default=None)
    p_diff.set_defaults(func=page_run_diff)

    # history
    p_hist = sub.add_parser("history", help="Show revision history for a page.")
    p_hist.add_argument("slug", nargs="?", default="", help="Optional slug to filter history.")
    p_hist.add_argument("--json", action="store_true", help="Output JSON.")
    p_hist.add_argument("--dest", default=None)
    p_hist.set_defaults(func=page_run_history)


def page_run_list(args: argparse.Namespace) -> int:
    import json

    from commontrace import knowledge_pages, paths

    root = paths.resolve_root(args.dest)
    pages = knowledge_pages.list_pages(root, tag=args.tag)
    if args.json:
        print(json.dumps([p.to_dict() for p in pages], indent=2))
        return 0
    if not pages:
        print("[commontrace] No knowledge pages found.")
        return 0
    print(f"[commontrace] {len(pages)} knowledge page(s):")
    for p in pages:
        v_str = f"v{p.version}"
        tags_str = f"[{', '.join(p.tags)}]" if p.tags else ""
        print(f"  {v_str:6s} {p.slug:32s} {p.title:28s} ({p.char_count} chars) {tags_str}")
    return 0


def page_run_tree(args):
    import json

    from commontrace import paths, wiki

    print(json.dumps(wiki.tree(paths.resolve_root(args.dest)), indent=2))
    return 0


def page_run_watch(args):
    import json

    from commontrace import paths, wiki

    print(
        json.dumps(
            wiki.register(paths.resolve_root(args.dest), args.slug, args.question, args.source, context=args.context),
            indent=2,
        )
    )
    return 0


def page_run_get(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import knowledge_pages, paths

    root = paths.resolve_root(args.dest)
    page = knowledge_pages.get_page(root, args.slug, version=args.version)
    if not page:
        print(f"[commontrace] Knowledge page '{args.slug}' not found.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(page.to_dict(), indent=2))
        return 0
    print(f"# {page.title} ({page.slug} · v{page.version})")
    print(f"Updated: {page.updated_at} by {page.last_actor}")
    if page.tags:
        print(f"Tags: {', '.join(page.tags)}")
    print("-" * 60)
    print(page.content)
    return 0


def page_run_set(args: argparse.Namespace) -> int:
    import sys

    from commontrace import knowledge_pages, paths

    root = paths.resolve_root(args.dest)
    try:
        res = knowledge_pages.update_page(
            root,
            args.slug,
            args.content,
            title=args.title,
            tags=args.tags,
            expected_version=args.expected_version,
            dry_run=args.dry_run,
            actor="cli",
            comment=args.comment,
        )
        if args.dry_run:
            print(
                f"[commontrace] Dry-run diff for '{args.slug}' "
                f"(v{res['current_version']} -> v{res['proposed_version']}):"
            )

            if res.get("diff"):
                print(res["diff"])
            else:
                print("  (No changes detected)")
        else:
            print(f"[commontrace] Saved page '{args.slug}' (v{res['version']})")
            if res.get("diff"):
                print(res["diff"])
        return 0
    except Exception as exc:
        print(f"[commontrace] Error: {exc}", file=sys.stderr)
        return 1


def page_run_diff(args: argparse.Namespace) -> int:
    args.dry_run = True
    args.title = ""
    args.tags = None
    args.expected_version = None
    args.comment = ""
    return page_run_set(args)


def page_run_history(args: argparse.Namespace) -> int:
    import json

    from commontrace import knowledge_pages, paths

    root = paths.resolve_root(args.dest)
    history = knowledge_pages.page_history(root, args.slug)
    if args.json:
        print(json.dumps(history, indent=2))
        return 0
    if not history:
        print(f"[commontrace] No history for '{args.slug}'.")
        return 0
    print(f"[commontrace] {len(history)} revision(s) for '{args.slug}':")
    for h in reversed(history):
        v = h.get("version", "?")
        actor = h.get("actor", "agent")
        ts = h.get("timestamp", "")[:19]
        comment = h.get("comment", "")
        print(f"  v{v:<4} [{ts}] by {actor:<10} - {comment}")
    return 0


# source operations
def source_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "source",
        help="Causally measure file-based agent memory (CLAUDE.md, AGENTS.md, "
        ".cursor/rules) that this product does not itself store.",
    )
    sub = p.add_subparsers(dest="source_cmd", required=True)

    sections = sub.add_parser(
        "sections",
        help="List the ## sections this file would be split into.",
    )
    sections.add_argument("path", help="Path to the memory file, e.g. CLAUDE.md")
    sections.add_argument("--json", action="store_true")
    sections.add_argument("--dest", default=None)
    sections.set_defaults(func=source_run_sections)

    render = sub.add_parser(
        "render",
        help="Render this occasion's version of the file: eligible sections "
        "not drawn into the withheld arm, blocked sections always excluded.",
    )
    render.add_argument("path")
    render.add_argument(
        "--occasion-id",
        required=True,
        help="Stable id for this session/task. The same id always gets the same "
        "answer; record its outcome afterwards with `source outcome`.",
    )
    render.add_argument("--out", default=None, help="Write the rendered text here instead of stdout.")
    render.add_argument("--dest", default=None)
    render.set_defaults(func=source_run_render)

    outcome = sub.add_parser(
        "outcome",
        help="Report whether the occasion this file was rendered for succeeded.",
    )
    outcome.add_argument("--occasion-id", required=True)
    group = outcome.add_mutually_exclusive_group(required=True)
    group.add_argument("--succeeded", action="store_true")
    group.add_argument("--failed", action="store_true")
    outcome.add_argument("--dest", default=None)
    outcome.set_defaults(func=source_run_outcome)

    withdraw = sub.add_parser(
        "withdraw",
        help="Permanently exclude a section from every future render (harm withdrawal). "
        "Never edits the source file -- see commontrace/memory_sources.py.",
    )
    withdraw.add_argument("path")
    withdraw.add_argument("--section-id", required=True)
    withdraw.add_argument("--dest", default=None)
    withdraw.set_defaults(func=source_run_withdraw)

    reinstate = sub.add_parser(
        "reinstate",
        help="Undo `withdraw` for one section.",
    )
    reinstate.add_argument("path")
    reinstate.add_argument("--section-id", required=True)
    reinstate.add_argument("--dest", default=None)
    reinstate.set_defaults(func=source_run_reinstate)


def source_run_sections(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import memory_sources, paths

    root = paths.resolve_root(args.dest)
    source = memory_sources.FileMemorySource(args.path, root=root)
    try:
        sections = source.sections()
    except OSError as exc:
        print(f"[commontrace] cannot read {args.path!r}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([{"id": s.id, "heading": s.heading} for s in sections], indent=2))
        return 0

    if not sections:
        print(f"[commontrace] no '## ' sections found in {args.path} (nothing to measure separately).")
        return 0
    for s in sections:
        print(f"  {s.id:<40} {s.heading}")
    return 0


def source_run_render(args: argparse.Namespace) -> int:
    import sys

    from commontrace import memory_sources, paths

    root = paths.resolve_root(args.dest)
    source = memory_sources.FileMemorySource(args.path, root=root)
    try:
        result = source.render(args.occasion_id)
    except OSError as exc:
        print(f"[commontrace] cannot read {args.path!r}: {exc}", file=sys.stderr)
        return 1

    if args.out:
        try:
            safe_out = paths.safe_prepare_output_path(args.out)
        except (OSError, ValueError) as exc:
            print(f"[commontrace] could not write {args.out!r}: {exc}", file=sys.stderr)
            return 1
        with open(safe_out, "w", encoding="utf-8") as fh:
            fh.write(result.text)
        print(f"[commontrace] wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(result.text)

    if result.blocked:
        print(f"[commontrace] blocked (harm-withdrawn, never drawn): {', '.join(result.blocked)}", file=sys.stderr)
    if result.withheld:
        print(f"[commontrace] withheld for this occasion: {', '.join(result.withheld)}", file=sys.stderr)
    print(
        f"[commontrace] record the outcome with: "
        f"commontrace source outcome --occasion-id {args.occasion_id} --succeeded|--failed",
        file=sys.stderr,
    )
    return 0


def source_run_outcome(args: argparse.Namespace) -> int:
    import sys

    from commontrace import holdout_io, paths

    root = paths.resolve_root(args.dest)
    try:
        wrote = holdout_io.record_outcome(root, args.occasion_id, bool(args.succeeded))
    except holdout_io.ConflictingOutcome as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if wrote:
        print(f"[commontrace] recorded occasion {args.occasion_id!r} as {'succeeded' if args.succeeded else 'failed'}.")
    else:
        print(f"[commontrace] occasion {args.occasion_id!r} was already recorded the same way.")
    return 0


def source_run_withdraw(args: argparse.Namespace) -> int:
    from commontrace import memory_sources, paths

    root = paths.resolve_root(args.dest)
    memory_sources.withdraw(root, args.path, args.section_id)
    print(f"[commontrace] {args.section_id!r} in {args.path} is now blocked from every future render.")
    return 0


def source_run_reinstate(args: argparse.Namespace) -> int:
    from commontrace import memory_sources, paths

    root = paths.resolve_root(args.dest)
    if memory_sources.reinstate(root, args.path, args.section_id):
        print(f"[commontrace] {args.section_id!r} in {args.path} is eligible again.")
    else:
        print(f"[commontrace] {args.section_id!r} in {args.path} was not blocked.")
    return 0


# agent operations
def agent_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "agent",
        help="Autonomous agent loop: run a multi-turn task with live working memory.",
    )
    sub = p.add_subparsers(dest="agent_cmd", required=True)

    run_p = sub.add_parser("run", help="Run an agent task.")
    run_p.add_argument("prompt", nargs="?", default="", help="Task prompt for the agent.")
    run_p.add_argument("--prompt", dest="prompt_opt", default=None, help="Task prompt for the agent (as an option).")
    run_p.add_argument("--prompt-file", default=None, help="Read prompt from a file.")
    run_p.add_argument("--max-turns", type=int, default=20, help="Max agent turns.")
    run_p.add_argument("--dest", default=None, help="CommonTrace store root.")
    run_p.add_argument("--dream", action="store_true", help="Trigger a dreaming consolidation pass after the run.")
    run_p.add_argument("--agent-type", default="agent", help="Agent type label for the trace.")
    run_p.add_argument("--json", dest="output_json", action="store_true", help="Output result as JSON.")
    run_p.set_defaults(func=agent_run)

    p.set_defaults(func=agent_run)


def agent_run(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import llm, paths
    from commontrace.agent_loop import AgentLoop, llm_executor

    root = paths.resolve_root(args.dest)

    prompt = (args.prompt_opt or args.prompt or "").strip()
    if args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as pf:
                prompt = pf.read().strip()
        except Exception as exc:
            print(f"[commontrace agent] could not read prompt file: {exc}", file=sys.stderr)
            return 1

    if not prompt:
        print("[commontrace agent] error: prompt is required (positional or --prompt-file).", file=sys.stderr)
        return 1

    try:
        executor = llm_executor()
    except llm.LLMUnavailable as exc:
        print(
            f"[commontrace agent] no model configured: {exc}\n"
            "  Set COMMONTRACE_LLM_PROVIDER / COMMONTRACE_LLM_API_KEY (or use `ollama`) to run an agent.",
            file=sys.stderr,
        )
        return 2
    agent_type = args.agent_type
    loop = AgentLoop(root=root, agent_type=agent_type)

    print(
        f"[commontrace agent] run: prompt_len={len(prompt)} max_turns={args.max_turns} "
        f"agent_type={agent_type!r} dest={root!r}",
        file=sys.stderr,
    )

    result = loop.run(
        prompt=prompt,
        tool_executor=executor,
        max_turns=args.max_turns,
        dream_on_complete=args.dream,
    )

    if args.output_json:
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "turns": len(result.turns),
                    "success": result.success,
                    "blocks_updated": result.blocks_updated,
                    "facts_recorded": result.facts_recorded,
                    "trace_path": result.trace_path,
                    "final_answer": result.final_answer[:500],
                },
                indent=2,
            )
        )
    else:
        print(f"[commontrace agent] run complete: {result.run_id[:12]}")
        print(f"  turns:           {len(result.turns)}")
        print(f"  success:         {result.success}")
        print(f"  blocks updated:  {result.blocks_updated}")
        print(f"  facts recorded:  {result.facts_recorded}")
        print(f"  trace:           {result.trace_path}")

    return 0 if result.success else 1


# recall operations
def recall_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "recall",
        help="Recall across every memory channel (lessons, facts, graph, conversations) into one budget.",
    )
    p.add_argument("question")
    p.add_argument("--budget", type=int, default=None, help="Token budget (default: memory/budgets.json or 1500).")
    p.add_argument("--agent", default=None, help="Use this agent's budget and weights from memory/budgets.json.")
    p.add_argument(
        "--channel",
        action="append",
        default=[],
        choices=("lessons", "facts", "graph", "conversations"),
        help="Only these channels (repeatable).",
    )
    p.add_argument("--as-of", default=None, help="Read every channel as it stood at this moment.")
    p.add_argument("--space", action="append", default=None, help="Conversation spaces (default: all).")
    p.add_argument("--embedder", default="auto", help="Conversation embedder: auto, none (lexical), or a tag.")
    p.add_argument(
        "--evidence-budget",
        type=int,
        default=0,
        help="Expand cited fact source quotes within this token allowance (default: disabled).",
    )
    p.add_argument("--scope", default="", help="Route lessons/facts to a scope or public memory.")
    p.add_argument(
        "--fact-scorer",
        default="overlap-v1",
        choices=("overlap-v1", "bm25-v1"),
        help="Fact ranking: compatible overlap or stemmed multilingual BM25.",
    )
    p.add_argument("--weight", action="append", default=[], metavar="CHANNEL=W", help="Channel weight override.")
    p.add_argument(
        "--reranker",
        default=None,
        help="Rerank the top fused items: cross-encoder, cross-encoder-fast, bge-reranker-v2-m3, "
        "mxbai-rerank, cohere[:model], voyage[:model], jina[:model], llm[:model] or none "
        "(default: memory/budgets.json, else none).",
    )
    p.add_argument("--rerank-depth", type=int, default=None, help="How many fused items the reranker reads (30).")
    p.add_argument(
        "--rerank-blend",
        type=float,
        default=None,
        help="Reranker weight beside the fused order: 0 keeps it, 1 replaces it (0.5).",
    )
    p.add_argument(
        "--adaptive-budget",
        action="store_true",
        default=None,
        help="Size the budget by the question's shape; grow it while evidence is thin.",
    )
    p.add_argument("--max-budget", type=int, default=None, help="With --adaptive-budget: the ceiling (12000).")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=recall_run)


def recall_run(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import paths, recall

    weights = {}
    for spec in args.weight:
        name, _, value = spec.partition("=")
        try:
            weights[name.strip()] = float(value)
        except ValueError:
            print(f"[commontrace] --weight expects CHANNEL=NUMBER, not {spec!r}", file=sys.stderr)
            return 2
    try:
        result = recall.recall(
            paths.resolve_root(args.dest),
            args.question,
            budget=args.budget,
            agent=args.agent,
            channels=tuple(args.channel) or recall.CHANNELS,
            as_of=args.as_of,
            weights=weights or None,
            spaces=args.space,
            embedder=args.embedder,
            evidence_budget=args.evidence_budget,
            scope=args.scope,
            fact_scorer=getattr(args, "fact_scorer", "overlap-v1"),
            reranker=getattr(args, "reranker", None),
            rerank_depth=getattr(args, "rerank_depth", None),
            rerank_blend=getattr(args, "rerank_blend", None),
            adaptive_budget=getattr(args, "adaptive_budget", None),
            max_budget=getattr(args, "max_budget", None),
        )
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    print(result.context or "(nothing relevant in memory)")
    found = ", ".join(f"{k} {v}" for k, v in result.considered.items())
    print(f"\n[commontrace] {result.tokens}/{result.budget} tokens; considered: {found or 'nothing'}", file=sys.stderr)
    for channel, error in result.errors.items():
        print(f"[commontrace] {channel} channel failed: {error}", file=sys.stderr)
    rerank = result.explain.get("rerank")
    if rerank:
        state = (
            f"failed ({rerank['error']}); fused order kept"
            if rerank.get("error")
            else f"read {rerank['items']} items in {rerank['latency_ms']} ms, moved {rerank.get('moved', 0)}"
        )
        print(f"[commontrace] reranker {rerank['reranker']} {state}", file=sys.stderr)
    return 0


# memory operations
def memory_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "memory",
        help="Version the store with git: validate, commit, diff, restore, repair conflicts, hand off.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    def cmd(name: str, help_: str, func) -> argparse.ArgumentParser:
        q = sub.add_parser(name, help=help_)
        q.add_argument("--dest", default=None)
        q.add_argument("--json", action="store_true", help="Print JSON.")
        q.set_defaults(func=func)
        return q

    cmd("init", "Make the store its own git repository and install the validating pre-commit hook.", memory_run_init)
    cmd("status", "Show the head commit and uncommitted memory changes.", memory_run_status)
    q = cmd("log", "Show recent memory commits.", memory_run_log)
    q.add_argument("-n", type=int, default=10)
    q = cmd("diff", "Show what changed in memory/ since a revision (default: uncommitted changes).", memory_run_diff)
    q.add_argument("rev", nargs="?", default=None)
    q = cmd("commit", "Validate and commit every memory change.", memory_run_commit)
    q.add_argument("-m", "--message", default="commontrace: memory update")
    q.add_argument("--no-verify", action="store_true", help="Skip validation and the pre-commit hook.")
    q = cmd("sign", "Sign the current committed memory tree with a detached HMAC receipt.", memory_run_sign)
    q.add_argument("--issuer", required=True)
    q = cmd("attach", "Attach shared read-only memory using an audience-bound handoff.", memory_run_attach)
    q.add_argument("shared_root")
    q.add_argument("--agent-id", required=True)
    q.add_argument("--handoff", required=True)
    q = cmd("restore", "Put memory/ back to a revision as a new commit (history is kept).", memory_run_restore)
    q.add_argument("rev")
    q = cmd("validate", "Check memory files against the limits in memory/memfs.json.", memory_run_validate)
    q.add_argument("--staged", action="store_true", help="Only the files staged for commit (the hook uses this).")
    cmd("repair", "Resolve merge conflicts: union JSONL logs, park the other side of anything else.", memory_run_repair)
    q = cmd(
        "keygen", "Write a handoff signing key to memory/.handoff_key (mode 0600, never committed).", memory_run_keygen
    )
    q.add_argument("--force", action="store_true")

    q = sub.add_parser("handoff", help="Hand a committed memory state to another agent, or check one.")
    hsub = q.add_subparsers(dest="action", required=True)
    h = hsub.add_parser("create", help="Sign a token naming the current memory commit.")
    h.add_argument("--to", required=True, dest="audience", help="Who may take the memory over.")
    h.add_argument("--from", default="", dest="issuer")
    h.add_argument("--ttl", type=int, default=3600, help="Seconds the token holds (60 to 30 days).")
    h.add_argument("--scope", action="append", default=[], help="Paths covered (default memory/; repeatable).")
    h.add_argument("--dest", default=None)
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=memory_run_handoff_create)
    h = hsub.add_parser("verify", help="Verify a handoff token against this store.")
    h.add_argument("token")
    h.add_argument("--audience", default=None, help="Refuse a token addressed to anyone else.")
    h.add_argument("--dest", default=None)
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=memory_run_handoff_verify)


def memory__guard(func):
    import sys

    from commontrace import memfs, paths

    def run(args: argparse.Namespace) -> int:
        try:
            return func(args, paths.resolve_root(args.dest))
        except memfs.MemfsError as exc:
            print(f"[commontrace] memory: {exc}", file=sys.stderr)
            return 1

    run.__name__ = func.__name__
    return run


def memory__emit(args: argparse.Namespace, data, text: str) -> None:
    import json

    print(json.dumps(data, indent=2, default=str) if args.json else text)


@memory__guard
def memory_run_init(args, root) -> int:
    from commontrace import memfs

    out = memfs.init(root)
    memory__emit(
        args,
        out,
        f"memory is versioned in {root}\nhook: {out['hook']}"
        + ("\ncommitted the current state" if out["committed"] else ""),
    )
    return 0


@memory__guard
def memory_run_status(args, root) -> int:
    from commontrace import memfs

    out = memfs.status(root)
    lines = [f"head: {out['head'] or '(no commits)'}"]
    lines += [f"  changed: {c}" for c in out["changed"]] or ["  clean"]
    memory__emit(args, out, "\n".join(lines))
    return 0


@memory__guard
def memory_run_log(args, root) -> int:
    from commontrace import memfs, memory_git

    memfs.require_repo(root)
    out = memory_git.log(root, args.n)
    if not out.get("ok"):
        raise memfs.MemfsError(out.get("error", "git log failed"))
    lines = [f"{e.get('hash', '')[:12]}  {e.get('date', '')}  {e.get('subject', '')}" for e in out["entries"]]
    memory__emit(args, out, "\n".join(lines) or "(no commits)")
    return 0


@memory__guard
def memory_run_diff(args, root) -> int:
    from commontrace import memfs

    text = memfs.diff(root, args.rev)
    memory__emit(args, {"diff": text}, text.rstrip() or "(no changes)")
    return 0


@memory__guard
def memory_run_commit(args, root) -> int:
    from commontrace import memfs

    out = memfs.commit(root, args.message, verify=not args.no_verify)
    memory__emit(args, out, f"committed {out['commit'][:12]}" if out.get("committed") else "nothing to commit")
    return 0


@memory__guard
def memory_run_sign(args, root) -> int:
    from commontrace import memfs

    out = memfs.sign_snapshot(root, issuer=args.issuer)
    memory__emit(args, out, f"signed memory commit {out['commit']}")
    return 0


@memory__guard
def memory_run_attach(args, root) -> int:
    from commontrace import memfs

    out = memfs.attach(root, args.shared_root, agent_id=args.agent_id, token=args.handoff)
    memory__emit(args, out, f"attached shared memory at {out['commit']} for {args.agent_id}")
    return 0


@memory__guard
def memory_run_restore(args, root) -> int:
    from commontrace import memfs

    out = memfs.restore(root, args.rev)
    if not out.get("ok"):
        raise memfs.MemfsError(out.get("error", "restore failed"))
    memory__emit(
        args,
        out,
        f"restored memory as {out['commit'][:12]}" if out.get("committed") else "memory already matches that revision",
    )
    return 0


@memory__guard
def memory_run_validate(args, root) -> int:
    import json
    import sys

    from commontrace import memfs, paths

    if args.staged:
        memfs.require_repo(root)
        files = [f for f in memfs._staged(root) if f.startswith(paths.memory_dir(root))]
        report = memfs.validate(root, files)
    else:
        report = memfs.validate(root)
    data = {"ok": report.ok, "checked": report.checked, "problems": report.problems}
    if args.json:
        print(json.dumps(data, indent=2))
    elif report.ok:
        print(f"memory ok ({report.checked} file(s) checked)")
    else:
        print("memory validation failed:", file=sys.stderr)
        for problem in report.problems:
            print(f"  {problem}", file=sys.stderr)
    return 0 if report.ok else 1


@memory__guard
def memory_run_repair(args, root) -> int:
    from commontrace import memfs

    out = memfs.repair(root)
    lines = [f"merged: {p}" for p in out["repaired"]] + [f"kept ours, theirs parked at: {p}" for p in out["parked"]]
    memory__emit(args, out, "\n".join(lines) or "no conflicts")
    return 0


@memory__guard
def memory_run_keygen(args, root) -> int:
    from commontrace import memfs

    path = memfs.keygen(root, force=args.force)
    memory__emit(
        args,
        {"key_file": path},
        f"wrote {path} (keep it out of version control; share it only with agents that should accept your handoffs)",
    )
    return 0


@memory__guard
def memory_run_handoff_create(args, root) -> int:
    from commontrace import memfs

    token = memfs.handoff(
        root, audience=args.audience, issuer=args.issuer, ttl_seconds=args.ttl, scope=args.scope or None
    )
    memory__emit(args, {"token": token}, token)
    return 0


@memory__guard
def memory_run_handoff_verify(args, root) -> int:
    from commontrace import memfs

    out = memfs.verify_handoff(root, args.token, audience=args.audience)
    text = f"valid handoff from {out['iss']} to {out['aud']} of {out['commit'][:12]}" + (
        "; memory has changed since" if out["drift"] else "; memory is unchanged since"
    )
    memory__emit(args, out, text)
    return 0


COMMANDS = {
    "kb": kb_add_parser,
    "community": community_add_parser,
    "observation": observation_add_parser,
    "procedural": procedural_add_parser,
    "ontology": ontology_add_parser,
    "page": page_add_parser,
    "source": source_add_parser,
    "agent": agent_add_parser,
    "recall": recall_add_parser,
    "memory": memory_add_parser,
}
