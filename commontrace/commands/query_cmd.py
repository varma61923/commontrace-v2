from __future__ import annotations

import argparse
import glob
import math
import os
import sys

from commontrace import (
    dosage,
    evidence_io,
    frontmatter,
    holdout_io,
    lesson_cache,
    paths,
    recency,
    redundancy,
    retrieval,
    retrieval_io,
    revision,
    store_state,
)
from commontrace.commands._format import read_or_warn
from commontrace.commands._shellout import has_attention_deps, run_script


def _relevance_floor(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--relevance-floor must be a number, got {raw!r}"
        ) from None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(
            f"--relevance-floor must be in [0.0, 1.0], got {value}"
        )
    return value


def _holdout_rate(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--holdout-rate must be a number, got {raw!r}"
        ) from None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(
            f"--holdout-rate must be in [0.0, 1.0], got {value}"
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
    p.add_argument("--holdout-rate", type=_holdout_rate, default=None)
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
    p.add_argument(
        "--exclude-shown", default=None, metavar="OCCASION_ID",
        help="Skip any lesson already logged as injected (not withheld) for this "
             "occasion in a prior `--experiment` call, so a long multi-turn task "
             "does not re-inject the same guidance on every call. Reads the holdout "
             "log (commontrace/holdout_io.py); a prior query for this occasion that "
             "did NOT use --experiment left no record, so nothing is excluded for it.",
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


def _already_shown(args: argparse.Namespace, root: str) -> set[str]:
    """Every slug `--exclude-shown`'s occasion has already been shown --
    see `holdout_io.injected_slugs_for_occasion`. Empty when the flag is
    not given, so a caller that never opts in pays no holdout-log read at
    all. Computed once per `commontrace query` call and threaded into both
    the lexical candidate filter and the semantic arm's output filter, so
    the two agree without reading the log twice.
    """
    if not args.exclude_shown:
        return set()
    return holdout_io.injected_slugs_for_occasion(root, args.exclude_shown)


def _exclude_shown(
    lessons: list[tuple[str, dict]],
    already_shown: set[str],
) -> list[tuple[str, dict]]:
    """Drop any MATCHED lesson in `already_shown`. Never drops a
    `core: true` lesson: core is the fleet's unconditional position
    (commontrace/dosage.py's module docstring), present every time by
    design -- "already shown" is not a reason to suppress it, the same way
    the redundancy check never suppresses one either.

    A no-op (returns `lessons` unchanged, by identity) when `already_shown`
    is empty.
    """
    if not already_shown:
        return lessons
    return [
        (path, fm) for path, fm in lessons
        if dosage.is_core(fm) or str(fm.get("name", "")) not in already_shown
    ]


def _ranking_adjustments(
    root: str,
    lessons: list[tuple[str, dict]],
    config: retrieval_io.RetrievalConfig,
) -> tuple[dict[str, float] | None, dict[str, float] | None]:
    """(reliability_lookup, recency_lookup) for `retrieval.rank_lessons`,
    each None when this store has not opted in (weight <= 0) -- so a store
    that has not configured either pays no extra cost at all: no evidence
    glob, no `last_hit` parsing, matching `dosage.select`'s own "tokenized
    lazily and only when the check is on" posture for redundancy.
    """
    reliability_lookup = (
        evidence_io.reliability_snapshot(root) if config.reliability_weight > 0 else None
    )
    recency_lu = (
        recency.recency_lookup(lessons) if config.recency_weight > 0 else None
    )
    return reliability_lookup, recency_lu


def _apply_dosage(
    active: list[tuple[str, dict]],
    ranked: list[tuple[str, float]],
    config: retrieval_io.RetrievalConfig,
) -> tuple[dict[str, dict], "dosage.Dose"]:
    """Admit core lessons and enforce this store's budget, CLI-side.

    Mirrors `commontrace/mcp_server.py`'s `_apply_dosage` -- same core
    admission, same budget, same optional redundancy suppression -- because
    before this the two retrieval surfaces disagreed about what an agent
    actually receives. An agent driving `commontrace query` by hand (or a
    script wrapping it) got every ranked lesson up to `--top-k`, unbounded
    by size and blind to `core: true`; the same store's agents retrieving
    over MCP got the budgeted, core-aware set. A fleet split across both
    surfaces was running two different treatments under one experiment.

    `active` is (path, frontmatter) as `lesson_cache` projects it -- missing
    `do_not_apply_when` and the body (see that module's docstring on what it
    deliberately does not cache), so every candidate considered here is
    re-read in full. That costs one `frontmatter.read` per candidate
    considered, not per lesson in the store: bounded by `--top-k` plus this
    store's core lessons, the same cost the MCP surface already pays per
    `retrieve()` call.

    `ranked` is (slug, relevance) in RANK ORDER for the matched set --
    `[(r.slug, r.relevance) for r in ranked]` from the lexical arm, or the
    fused `(slug, score)` pairs from `_run_hybrid`. Retrieval has already
    ranked; this function does not re-sort it.

    Returns (considered, dose) where `considered` maps slug -> the item
    actually read (slug, path, relevance, core, fm, body) -- including ones
    the dose dropped, so a caller can still describe what a dropped slug
    was. Look up `dose.admitted` (by slug, against this dict) for what to
    inject.
    """
    path_by_slug = {str(fm.get("name", "")): path for path, fm in active}
    core_slugs_all = {str(fm.get("name", "")) for path, fm in active if dosage.is_core(fm)}
    ranked_slugs = {slug for slug, _ in ranked}

    considered: dict[str, dict] = {}

    def _consider(slug: str, relevance: float) -> None:
        if slug in considered:
            return
        path = path_by_slug.get(slug)
        if not path:
            return
        parsed = read_or_warn(frontmatter.read, path)
        if parsed is None:
            return
        fm, body = parsed
        considered[slug] = {
            "slug": slug, "path": path, "relevance": relevance,
            "core": slug in core_slugs_all, "fm": fm, "body": body,
        }

    # Core lessons that did not also match are considered first, so they
    # compete for the budget ahead of the ranked set -- `dosage.select`
    # re-sorts core candidates by importance regardless of this order, but
    # feeding it their true priority keeps this function's own bookkeeping
    # (and any caller that inspects `considered` before calling `select`)
    # honest about why a core lesson is here at all.
    for slug in sorted(core_slugs_all - ranked_slugs):
        _consider(slug, 0.0)
    for slug, relevance in ranked:
        _consider(slug, relevance)

    candidates = [
        dosage.Candidate(
            slug=item["slug"],
            text=item["body"],
            core=item["core"],
            relevance=item["relevance"],
            importance=int(item["fm"].get("importance") or 0),
            revision=revision.revision_of(item["fm"], item["body"]) or "",
            # Same fields `commontrace consolidate` and the MCP surface
            # compare on -- see redundancy.py's module docstring for why
            # tags/domain are deliberately excluded.
            compare_text=redundancy.comparable_text(item["fm"], item["body"]),
        )
        for item in considered.values()
    ]
    dose = dosage.select(
        candidates,
        dosage.Budget(
            max_lessons=config.max_lessons,
            max_chars=config.max_chars,
            redundancy_threshold=config.redundancy_threshold,
        ),
    )
    return considered, dose


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
    lessons = _exclude_shown(lessons, _already_shown(args, root))
    config = retrieval_io.load_config(root)
    floor = config.floor if args.relevance_floor is None else args.relevance_floor
    reliability_lookup, recency_lu = _ranking_adjustments(root, lessons, config)
    ranked = retrieval.rank_lessons(
        args.task, lessons, top_k=args.top_k, floor=floor, scorer=config.scorer,
        term_cache=term_cache,
        reliability_lookup=reliability_lookup, reliability_weight=config.reliability_weight,
        recency_lookup=recency_lu, recency_weight=config.recency_weight,
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
        # Which of the four "nothing came back" cases this is, and the one
        # command that moves the caller forward -- see commontrace/store_state.py.
        # The old message said "try lesson list" unconditionally, which shows
        # an empty list in exactly the cases where the user is most lost.
        print(store_state.why_no_results(root, searched="query"))
        return 0

    # Core lessons admitted, the budget enforced, redundant restatements
    # dropped if this store opted in -- commontrace/dosage.py, the same
    # allocation the MCP surface applies to every `retrieve()` call. Before
    # this, `--top-k` was the only limit here: a store with `core: true`
    # lessons or a character budget configured got a DIFFERENT set of
    # lessons from this command than from its own agents retrieving over
    # MCP, which is two treatments under one experiment.
    ranked_by_slug = {r.slug: r for r in ranked}
    considered, dose = _apply_dosage(
        lessons, [(r.slug, r.relevance) for r in ranked], config,
    )
    if not dose.admitted:
        print(
            f"[commontrace] {len(ranked)} lesson(s) matched, but this store's injection "
            f"budget ({dose.gauge()}) admitted none. Widen it with `commontrace retrieval "
            "--max-lessons`/`--max-chars`. Dropped: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
        return 0

    # BEFORE the arms are assigned, and in the same order MCP's own
    # `retrieve()` uses (see that module's `_apply_dosage`): a lesson the
    # budget crowds out is never administered, so it must never be eligible
    # for randomization either. Assigning it an arm anyway would log an
    # occasion as treated where no memory was actually injected, pulling
    # the measured effect toward zero -- silently, and worse the tighter
    # the budget. Core lessons stay excluded from randomization: they are
    # unconditional by definition and present in both arms, so they cannot
    # confound the comparison.
    eligible = [c.slug for c in dose.admitted if not c.core]

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
            args, root, eligible,
            relevance={c.slug: c.relevance for c in dose.admitted},
            scorer=config.scorer,
            floor=floor,
        )

    for c in dose.admitted:
        if c.slug in withheld:
            # Printed rather than hidden so a human driving this can see the
            # experiment is running. An automated retriever should skip these.
            print(f"{c.slug:45s} [WITHHELD - holdout]")
            continue
        r = ranked_by_slug.get(c.slug)
        if r is not None:
            print(f"{c.slug:45s} rel={r.relevance:4.2f}  {r.description}")
            print(f"  matched: {', '.join(r.matched_terms)}  ({r.path})")
        else:
            # A core lesson admitted alongside the ranked set rather than
            # because it matched today's task -- see commontrace/dosage.py.
            item = considered[c.slug]
            print(f"{c.slug:45s} [core]  {item['fm'].get('description', '')}")
            print(f"  ({item['path']})")

    if dose.dropped:
        # Never silent: an agent given nine of ten lessons and told it was
        # given ten acts on the missing one's absence as though it were the
        # fleet's position.
        print(
            "\n[commontrace] not injected: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
    print(f"[commontrace] budget: {dose.gauge()}")

    if args.experiment:
        print(
            f"\n[commontrace] experiment: {len(dose.admitted) - len(withheld)} injected, "
            f"{len(withheld)} withheld at {_effective_holdout(args, root)[0]:.0%} for occasion "
            f"{args.occasion_id!r}. Record the outcome under that id, then run "
            "`commontrace experiment`."
        )
    return 0



def _semantic_slugs(args, root: str, missing_hint: str) -> tuple[int, list[str], str]:
    """Run the semantic arm and return (rc, ranked slugs, raw stdout)."""
    script_args = ["--top-k", str(args.top_k)]
    if args.include_importance_floor is not None:
        script_args.extend(
            ["--include-importance-floor", str(args.include_importance_floor)])
    if args.agent_type:
        script_args.extend(["--agent-type", args.agent_type])
    script_args.extend(["--", args.task])
    rc, stdout = run_script(
        root, os.path.join("memory", "attention", "query.py"),
        script_args, missing_hint, capture=True,
    )
    if rc != 0:
        return rc, [], stdout
    return 0, _slugs_from_semantic_output(stdout), stdout


def _run_hybrid(args: argparse.Namespace, root: str, missing_hint: str) -> int:
    """Both arms, fused by position (commontrace/retrieval.py).

    WHY FUSE RATHER THAN CHOOSE. Until now this command picked ONE retriever:
    semantic when the extra was installed and the index was fresh, lexical
    otherwise. Whichever it picked, the other arm's signal was discarded
    entirely -- so a store with the attention extra could not find a lesson
    whose exact error string the user had pasted in, and a store without it
    could not find one phrased differently from the task. They fail on
    different queries, which is precisely the condition under which fusing
    beats picking.

    Fusion is by RANK, not score: the lexical arm returns an IDF relevance in
    [0, 1] and the semantic arm a cosine similarity, and there is no honest
    conversion between them. Position is the one thing both arms can state
    comparably.

    A lesson only one arm surfaced is not penalised for the other arm's
    silence -- a lexical pass cannot be expected to find a paraphrase, and
    treating its silence as a vote against would make adding an arm reduce
    recall.
    """
    config = retrieval_io.load_config(root)
    floor = config.floor if args.relevance_floor is None else args.relevance_floor

    lessons, term_cache = lesson_cache.load_active_with_terms(
        root, args.agent_type, reader=lambda p: read_or_warn(frontmatter.read, p),
    )
    already_shown = _already_shown(args, root)
    lessons = _exclude_shown(lessons, already_shown)
    reliability_lookup, recency_lu = _ranking_adjustments(root, lessons, config)
    lexical = retrieval.rank_lessons(
        args.task, lessons, top_k=args.top_k, floor=floor, scorer=config.scorer,
        term_cache=term_cache,
        reliability_lookup=reliability_lookup, reliability_weight=config.reliability_weight,
        recency_lookup=recency_lu, recency_weight=config.recency_weight,
    )

    rc, semantic, stdout = _semantic_slugs(args, root, missing_hint)
    if already_shown and rc == 0:
        # The semantic arm runs as a separate subprocess
        # (memory/attention/query.py) with no knowledge of --exclude-shown,
        # so a lesson dropped from the lexical candidate set above can
        # still come back through this arm and reach the fused result
        # unfiltered. Same exclusion, same core-lesson exemption, applied
        # to this arm's output instead of its input.
        core_slugs = {str(fm.get("name", "")) for _p, fm in lessons if dosage.is_core(fm)}
        semantic = [s for s in semantic if s in core_slugs or s not in already_shown]
    if rc != 0:
        # The semantic arm failed outright. Serving the lexical half is
        # strictly better than serving nothing, but the arm composition is
        # then NOT what the config says -- and an assignment logged under the
        # fused label would claim an arm that did not run. So fall back
        # wholesale, which records the lexical label.
        sys.stdout.write(stdout)
        print(
            "[commontrace] the semantic arm failed, so this query used lexical "
            "retrieval alone. The holdout assignment records the lexical "
            "configuration, not the fused one -- the two are different "
            "treatments and must not be pooled.",
            file=sys.stderr,
        )
        return _run_lexical(args, root)

    fused = retrieval.reciprocal_rank_fusion(
        {"lexical": [r.slug for r in lexical], "semantic": semantic},
        k=config.rrf_k, top_k=args.top_k,
    )
    if not fused:
        print(store_state.why_no_results(root, searched="query"))
        return 0

    by_slug = {r.slug: r for r in lexical}
    described = {
        str(fm.get("name", "")): (str(fm.get("description", "") or ""), path)
        for path, fm in lessons
    }

    # Same budget the lexical path and the MCP surface apply -- see
    # `_apply_dosage`'s docstring and `_run_lexical` above, which this
    # mirrors line for line. `described` (built from the full active set
    # above) already covers a core-only admission, so the `considered` map
    # `_apply_dosage` returns is not needed again here.
    _considered, dose = _apply_dosage(lessons, fused, config)
    if not dose.admitted:
        print(
            f"[commontrace] {len(fused)} lesson(s) matched, but this store's injection "
            f"budget ({dose.gauge()}) admitted none. Widen it with `commontrace retrieval "
            "--max-lessons`/`--max-chars`. Dropped: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
        return 0

    eligible = [c.slug for c in dose.admitted if not c.core]

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
            args, root, eligible,
            # The FUSED score, which is what actually decided the order --
            # recording the lexical relevance would describe a ranking this
            # query did not perform.
            relevance={c.slug: c.relevance for c in dose.admitted},
            scorer=config.eligibility,
            floor=floor,
        )

    fused_score_by_slug = dict(fused)
    for c in dose.admitted:
        slug = c.slug
        if slug in withheld:
            print(f"{slug:45s} [WITHHELD - holdout]")
            continue
        description, path = described.get(slug, ("", ""))
        arms = []
        if slug in by_slug:
            arms.append("lexical")
        if slug in semantic:
            arms.append("semantic")
        score_label = (
            f"rrf={fused_score_by_slug[slug]:5.3f}" if slug in fused_score_by_slug
            # A core lesson admitted alongside the fused set rather than
            # because either arm ranked it -- see commontrace/dosage.py.
            else "[core]     "
        )
        print(f"{slug:45s} {score_label}  {description}")
        print(f"  arms: {'+'.join(arms) or 'none'}  ({path})")

    if dose.dropped:
        print(
            "\n[commontrace] not injected: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
    print(f"[commontrace] budget: {dose.gauge()}")

    if args.experiment:
        print(
            f"\n[commontrace] experiment: {len(dose.admitted) - len(withheld)} injected, "
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

    # Both arms, fused -- but only when the store has opted in. Turning this
    # on changes which lessons are eligible, which is the denominator of any
    # running experiment, so it is a decision the store records rather than
    # something a new release switches on underneath a pilot.
    if retrieval_io.load_config(root).fusion == retrieval_io.FUSION_RRF:
        return _run_hybrid(args, root, missing_hint)

    script_args = ["--top-k", str(args.top_k)]
    if args.include_importance_floor is not None:
        script_args.extend(["--include-importance-floor", str(args.include_importance_floor)])
    if args.agent_type:
        # Forwarded now that the index carries an agent_types column. It used
        # to be dropped with a warning, which meant one organisation running
        # several fleets out of one store could scope lexical retrieval to a
        # fleet and not semantic retrieval -- two retrievers answering
        # different questions from the same store.
        script_args.extend(["--agent-type", args.agent_type])
    script_args.extend(["--", args.task])
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
