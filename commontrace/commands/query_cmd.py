from __future__ import annotations

import argparse
import glob
import os
import sys

from commontrace import (
    frontmatter,
    holdout_io,
    lesson_cache,
    paths,
    retrieval,
    retrieval_io,
)
from commontrace.commands._format import read_or_warn
from commontrace.commands._shellout import has_attention_deps, run_script


def _relevance_floor(raw: str) -> float:
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(
            f"--relevance-floor must be in [0.0, 1.0], got {value}"
        )
    return value


def _positive_int(raw: str) -> int:
    """argparse type= for --top-k: a Python slice silently accepts a
    negative count (`order[:-1]` is "all but the last", not an error), so
    `--top-k -1` returned nearly the ENTIRE index instead of failing --
    the opposite of what a caller asking for "a small number of results"
    intended. Rejected here, at parse time, rather than clamped silently:
    a negative top-k is a caller bug worth surfacing, not a value with a
    sensible default to fall back to."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"--top-k must be >= 1, got {value}")
    return value


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "query",
        help="Retrieve top-K relevant lessons for a task (semantic pre-filter, "
        "or a lexical fallback with only the core install).",
    )
    p.add_argument("task", help="Incoming task / query string")
    p.add_argument("--top-k", type=_positive_int, default=10)
    p.add_argument(
        "--lexical", action="store_true",
        help="Force the pure-Python lexical fallback even if the attention extra is installed.",
    )
    p.add_argument("--agent-type", default=None)
    p.add_argument(
        "--relevance-floor", type=_relevance_floor, default=None,
        help="Minimum relevance (0-1) a lesson must reach to be retrieved at all. "
             "Defaults to this store's configured floor (`commontrace retrieval`). "
             "Under --experiment this also decides which lessons are logged as "
             "eligible, so lowering it admits weak matches into the causal estimate.",
    )
    p.add_argument(
        "--experiment", action="store_true",
        help="Randomized holdout mode: deliberately withhold a fraction of otherwise-"
        "matching lessons and log the assignment, so `commontrace experiment` can later "
        "measure whether injection actually CAUSES better outcomes. Without this, every "
        "lesson-value number is correlational and confounded by which situations trigger "
        "each lesson.",
    )
    p.add_argument(
        "--occasion-id", default=None,
        help="Identifier for this decision, used to join the holdout assignment to its "
        "outcome later. Must match the episode `name` or trace `id` you record afterwards.",
    )
    # Default None, resolved against the STORE's configured experiment at run
    # time (_apply_holdout). A flag defaulting to a module constant is how the
    # two retrieval surfaces came to disagree: `query` used 10% while the same
    # fleet's agents retrieved over MCP at 10% from a different constant, and
    # any operator who set one and not the other pooled two randomizations
    # into one comparison. Passing either flag explicitly still overrides,
    # which is what a one-off experiment needs.
    p.add_argument("--holdout-rate", type=float, default=None)
    p.add_argument("--experiment-salt", default=None)
    p.add_argument(
        "--include-importance-floor",
        type=int,
        default=None,
        help=(
            "Always include lessons with importance >= this floor "
            "(safety override, default: 4 in semantic retriever)."
        ),
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _iter_active_lessons(root: str, agent_type: str | None) -> list[tuple[str, dict]]:
    """Active lessons as (path, frontmatter), in the store's own path order.

    Served from `commontrace/lesson_cache.py`, which reparses only the files
    whose (mtime, size) changed. This used to YAML-parse the whole store on
    every query: at 6,400 lessons that was 7.3 s of parsing per query against
    0.17 s of actual ranking, growing linearly (see that module's docstring for
    the measurements and `commontrace/reference/measure_local_latency.py` to
    reproduce them). Staleness is still detected per query by stat, so the
    ranking always reflects the store as it is right now.
    """
    return lesson_cache.load_active(
        root, agent_type, reader=lambda p: read_or_warn(frontmatter.read, p),
    )


def _apply_holdout(
    args: argparse.Namespace,
    root: str,
    slugs: list[str],
    relevance: dict[str, float] | None = None,
    scorer: str = "",
    floor: float | None = None,
) -> set[str]:
    """Thin wrapper over holdout_io.assign_and_log -- see that function.

    The body used to live here, which meant any second retriever (the MCP
    server an agent talks to, for one) would have had to reimplement arm
    assignment. Two implementations of a randomized assignment is two
    chances to bias the causal number this whole experiment exists to
    produce, so there is now exactly one.
    """
    rate, salt = _effective_holdout(args, root)
    return holdout_io.assign_and_log(
        root, slugs, occasion_id=args.occasion_id, rate=rate, salt=salt,
        relevance=relevance, scorer=scorer, floor=floor,
    )


def _effective_holdout(args: argparse.Namespace, root: str) -> tuple[float, str]:
    """The rate and salt this run assigns with.

    One resolver, used by the assignment and by every line that reports what
    it did. Two of those lines used to read `args.holdout_rate` directly,
    which was fine while the flag defaulted to a constant and became a crash
    the moment it defaulted to "whatever the store is configured for" -- and
    would have been a quietly WRONG printed rate if the None had happened to
    format.
    """
    config = holdout_io.load_config(root)
    return (
        config.rate if args.holdout_rate is None else args.holdout_rate,
        config.salt if args.experiment_salt is None else args.experiment_salt,
    )


def _slug_of_semantic_line(line: str) -> str | None:
    """The slug from one `memory/attention/query.py` result line, or None.

    That script emits `<slug> | cosine=<x> | importance=<n>` for hits and
    `#`-prefixed header/warning lines for everything else.
    """
    if line.startswith("#") or "|" not in line:
        return None
    slug = line.split("|", 1)[0].strip()
    return slug or None


def _slugs_from_semantic_output(stdout: str) -> list[str]:
    seen: list[str] = []
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        # A slug can legitimately appear twice (top-k hit plus importance-floor
        # override); the holdout must treat it as one eligible lesson.
        if slug is not None and slug not in seen:
            seen.append(slug)
    return seen


def _run_lexical(args: argparse.Namespace, root: str) -> int:
    lessons, term_cache = lesson_cache.load_active_with_terms(
        root, args.agent_type, reader=lambda p: read_or_warn(frontmatter.read, p),
    )
    config = retrieval_io.load_config(root)
    floor = config.floor if args.relevance_floor is None else args.relevance_floor
    ranked = retrieval.rank_lessons(
        args.task, lessons, top_k=args.top_k, floor=floor, scorer=config.scorer,
        term_cache=term_cache,
    )
    # Only when the pin is an actual DOWNGRADE. A store already running the
    # current scorer is also "pinned" (to what its own log says it uses), and
    # saying so on every query would be noise nobody can act on -- and noise
    # is how the one message that does need acting on gets ignored.
    if config.pinned_for_running_experiment and config.scorer != retrieval.SCORER_IDF:
        # Said once, where someone can act on it, rather than silently
        # upgrading a store whose experiment is mid-flight.
        print(
            "[commontrace] note: this store has holdout assignments already recorded, so "
            f"retrieval stays on the {config.scorer!r} scorer those assignments were made "
            "under.\n"
            "  Switching scorers changes which lessons are eligible, which would pool two "
            "different treatments\n"
            "  into one comparison. To adopt the field-robust scorer, finish or restart the "
            "experiment:\n"
            "    commontrace retrieval --scorer idf-v2 && commontrace experiment --configure "
            "--rate <rate>",
            file=sys.stderr,
        )
    if not ranked:
        print("[commontrace] no lexical matches. Try `commontrace lesson list` for a full view.")
        return 0

    withheld: set[str] = set()
    if args.experiment:
        if not args.occasion_id:
            print(
                "[commontrace] --experiment requires --occasion-id: without it the holdout "
                "assignment cannot be joined to an outcome, so nothing could be measured.",
                file=sys.stderr,
            )
            return 1
        withheld = _apply_holdout(
            args, root, [r.slug for r in ranked],
            relevance={r.slug: r.relevance for r in ranked},
            scorer=config.scorer,
            floor=floor,
        )

    for r in ranked:
        if r.slug in withheld:
            # Printed rather than hidden so a human driving this can see the
            # experiment is running. An automated retriever should skip these.
            print(f"{r.slug:45s} [WITHHELD - holdout]")
            continue
        print(f"{r.slug:45s} rel={r.relevance:4.2f}  {r.description}")
        print(f"  matched: {', '.join(r.matched_terms)}  ({r.path})")

    if args.experiment:
        print(
            f"\n[commontrace] experiment: {len(ranked) - len(withheld)} injected, "
            f"{len(withheld)} withheld at {_effective_holdout(args, root)[0]:.0%} for occasion "
            f"{args.occasion_id!r}. Record the outcome under that id, then run "
            "`commontrace experiment`."
        )
    return 0


def _index_is_unusable(root: str) -> str:
    """Why the semantic index cannot be trusted right now, or "" if it can.

    Deliberately cheap and dependency-free -- mtimes and file size, no numpy,
    no model load. build_index.py's own check is stricter (it compares the
    indexed slug SET and the embedding model), but it can only run after
    importing numpy and is therefore not something `query` can consult on
    every call. This catches the two cases that matter in practice: no index
    was ever built, and a lesson changed since the last build.

    Used to pick the retriever BEFORE paying for a model load, because the
    alternative is worse than slow. `commontrace init` writes an EMPTY
    index.npz, and nothing rebuilds it automatically, so a fleet that
    approves lessons and queries -- the normal first hour with this product
    -- ran semantic retrieval against an index containing nothing, got zero
    results, and under `--experiment` logged NO assignment for the occasion.
    The pilot silently lost the occasion and exited 0.
    """
    index_path = os.path.join(paths.memory_dir(root), "attention", "index.npz")
    try:
        index_mtime = os.path.getmtime(index_path)
    except OSError:
        return "no semantic index has been built yet"

    newest_lesson = 0.0
    newest_name = ""
    for path in glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md")):
        if os.path.basename(path) == "lesson_template.md":
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_lesson:
            newest_lesson, newest_name = mtime, os.path.basename(path)
    if newest_lesson == 0.0:
        # No lessons on disk but possibly a stale non-empty index (e.g. all
        # lessons deleted after a build): trusting it would rank ghosts.
        return "no active lessons on disk (a stale index would rank ghosts)"
    if newest_lesson > index_mtime:
        return f"{newest_name} changed after the index was last built"
    # Deletions of 1-of-N advance no survivor's mtime, so this gate cannot
    # see them without reading the index (which needs numpy); build_index.py
    # performs the full slug-set comparison at build time, and `commontrace
    # index` after deleting lessons is the supported refresh path.
    return ""


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)

    if not args.lexical and has_attention_deps():
        reason = _index_is_unusable(root)
        if reason:
            # Fall back to the retriever that is correct right now rather than
            # to silence. Lexical reads the lesson files themselves, so it
            # cannot be stale, needs no index and no model -- and a fleet that
            # keeps retrieving is strictly better than one that keeps
            # returning nothing while its experiment quietly accrues no data.
            print(
                f"[commontrace] semantic index unusable ({reason}); using lexical "
                "retrieval for this query.\n"
                "  Rebuild it with `commontrace index` to use semantic retrieval.",
                file=sys.stderr,
            )
            return _run_lexical(args, root)

    if args.lexical or not has_attention_deps():
        if not args.lexical:
            print(
                "[commontrace] Semantic retrieval requires the optional attention extra "
                "(numpy + sentence-transformers): `pip install commontrace[attention]`. "
                "Falling back to lexical (word-overlap) retrieval.",
                file=sys.stderr,
            )
        return _run_lexical(args, root)

    missing_hint = (
        "The reference attention scripts ship inside the package, so this means a "
        "damaged install -- try `pip install --force-reinstall commontrace`. "
        "Falling back: `commontrace query --lexical`, or `commontrace lesson list` for a full view."
    )
    script_args = [args.task, "--top-k", str(args.top_k)]
    if args.include_importance_floor is not None:
        script_args.extend(["--include-importance-floor", str(args.include_importance_floor)])
    if args.agent_type:
        # Forwarded now that the index carries an agent_types column. It used
        # to be dropped with a warning, which meant one organisation running
        # several fleets out of one store could scope lexical retrieval to a
        # fleet and not semantic retrieval -- two retrievers answering
        # different questions from the same store.
        script_args.extend(["--agent-type", args.agent_type])
    script_path = os.path.join("memory", "attention", "query.py")

    if not args.experiment:
        return run_script(root, script_path, script_args, missing_hint)

    if not args.occasion_id:
        print(
            "[commontrace] --experiment requires --occasion-id: without it the holdout "
            "assignment cannot be joined to an outcome, so nothing could be measured.",
            file=sys.stderr,
        )
        return 1

    # The holdout has to apply on BOTH retrieval paths. Previously only the
    # lexical branch honoured it, so a fleet with the attention extra
    # installed -- the recommended production setup -- ran `--experiment` and
    # silently measured nothing: no arms were ever logged, and `commontrace
    # experiment` reported "no holdout assignments recorded yet" forever.
    #
    # Rather than duplicate the assignment logic into the reference script,
    # capture what it ranked and apply the same _apply_holdout the lexical
    # path uses. Ranking stays in one place; arm assignment stays in one place.
    rc, stdout = run_script(root, script_path, script_args, missing_hint, capture=True)
    if rc != 0:
        sys.stdout.write(stdout)
        return rc

    slugs = _slugs_from_semantic_output(stdout)
    if not slugs:
        sys.stdout.write(stdout)
        print(
            "[commontrace] --experiment: the semantic retriever returned no lessons, "
            "so no holdout arms were recorded for this occasion.",
            file=sys.stderr,
        )
        return 0

    withheld = _apply_holdout(args, root, slugs)
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        if slug is not None and slug in withheld:
            print(f"{slug} | [WITHHELD - holdout]")
        else:
            print(line)
    print(
        f"\n[commontrace] experiment: {len(slugs) - len(withheld)} injected, "
        f"{len(withheld)} withheld at {_effective_holdout(args, root)[0]:.0%} for occasion "
        f"{args.occasion_id!r}. Record the outcome under that id, then run "
        "`commontrace experiment`."
    )
    return 0
