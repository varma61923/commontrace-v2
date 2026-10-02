from __future__ import annotations

import argparse
import math
import os
import re
import sys

from commontrace import (
    dosage,
    evidence,
    evidence_io,
    frontmatter,
    harm,
    holdout_io,
    injection_guard,
    lesson_cache,
    paths,
    recency,
    redundancy,
    rerank_arm,
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
        "--scope", default="",
        help="Retrieve lessons for this project/team scope plus unscoped global lessons.",
    )
    p.add_argument(
        "--as-of", default="", metavar="DATE",
        help="Retrieve lessons valid at this date/time (YYYY-MM-DD or ISO 8601). Defaults to now.",
    )
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


def _iter_active_lessons(
    root: str, agent_type: str | None, scope: str = "", as_of: str = "",
) -> list[tuple[str, dict]]:
    lessons = lesson_cache.load_active(
        root, agent_type, reader=lambda p: read_or_warn(frontmatter.read, p),
    )
    return lesson_cache.filter_eligible(lessons, scope=scope, as_of=as_of or None)


def _already_shown(args: argparse.Namespace, root: str) -> set[str]:
    if not args.exclude_shown:
        return set()
    return holdout_io.injected_slugs_for_occasion(root, args.exclude_shown)


def _exclude_shown(
    lessons: list[tuple[str, dict]],
    already_shown: set[str],
) -> list[tuple[str, dict]]:
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
    reliability_lookup = (
        evidence_io.reliability_snapshot(root) if config.reliability_weight > 0 else None
    )
    recency_lu = (
        recency.recency_lookup(lessons) if config.recency_weight > 0 else None
    )
    return reliability_lookup, recency_lu


def _core_slugs(lessons: list[tuple[str, dict]]) -> set[str]:
    return {str(fm.get("name", "")) for _path, fm in lessons if dosage.is_core(fm)}


def _print_withdrawn(slugs: list[str], harmful: dict[str, dict]) -> None:
    if not slugs:
        return
    parts = []
    for slug in slugs:
        ev = harmful.get(slug, {})
        effect = ev.get("effect")
        lo, hi = (ev.get("ci_95") or [None, None])[:2]
        figure = f"effect {effect:+.1%}" if isinstance(effect, (int, float)) else "HURTS"
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            figure += f", 95% CI {lo:+.1%} to {hi:+.1%}"
        parts.append(f"{slug} ({figure})")
    print("\n[commontrace] withdrawn -- measured to make outcomes worse, so not "
          "injected (commontrace retrieval --on-harm): " + ", ".join(parts))


def _withdraw_from_semantic(
    stdout: str, harmful: dict[str, dict], core: set[str], top_k: int,
) -> tuple[str, list[str]]:
    if not harmful:
        return stdout, []
    kept_slugs, removed_slugs = harm.split(
        _slugs_from_semantic_output(stdout), harmful, core, top_k, slug_of=lambda s: s,
    )
    keep = set(kept_slugs)
    lines = []
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        if slug is None or slug in keep:
            lines.append(line)
    return "\n".join(lines) + ("\n" if stdout.endswith("\n") else ""), removed_slugs


def _screen_semantic(stdout: str, root: str) -> str:
    ldir = paths.lessons_dir(root)
    verdicts: dict[str, bool] = {}
    lines = []
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        if slug is not None:
            if slug not in verdicts:
                path = os.path.join(ldir, f"{slug}.md")
                parsed = read_or_warn(frontmatter.read, path) if os.path.isfile(path) else ({}, "")
                labels = [] if parsed is None else injection_guard.injection_labels({
                    "description": parsed[0].get("description"), "applies_when": parsed[0].get("applies_when"),
                    "do_not_apply_when": parsed[0].get("do_not_apply_when"), "body": parsed[1]})
                verdicts[slug] = parsed is not None and not labels
                if labels:
                    print(f"[commontrace] quarantined {slug}: injection screen: {', '.join(labels)}",
                          file=sys.stderr)
            if not verdicts[slug]:
                continue
        lines.append(line)
    return "\n".join(lines) + ("\n" if stdout.endswith("\n") else "")


def _semantic_dose_or_pinned(
    stdout: str, root: str, agent_type: str | None, config: retrieval_io.RetrievalConfig, dosed: bool,
    scope: str = "", as_of: str = "",
) -> tuple[str, list[str], str]:
    active = _iter_active_lessons(root, agent_type, scope, as_of)
    if dosed:
        return _dose_semantic(stdout, root, agent_type, config, active=active)
    stdout = _screen_semantic(stdout, root)
    allowed = {str(fm.get("name", "")) for _path, fm in active}
    lines = [
        line for line in stdout.splitlines()
        if (slug := _slug_of_semantic_line(line)) is None or slug in allowed
    ]
    stdout = "\n".join(lines) + ("\n" if stdout.endswith("\n") else "")
    return stdout, _slugs_from_semantic_output(stdout), ""


def _dose_semantic(
    stdout: str, root: str, agent_type: str | None, config: retrieval_io.RetrievalConfig,
    active: list[tuple[str, dict]] | None = None,
) -> tuple[str, list[str], str]:
    lines = stdout.splitlines()
    order = list(dict.fromkeys(_slugs_from_semantic_output(stdout)))
    active = _iter_active_lessons(root, agent_type) if active is None else active
    on_disk = {str(fm.get("name", "")) for _p, fm in active}
    cosine: dict[str, float] = {}
    for line in lines:
        slug = _slug_of_semantic_line(line)
        if slug is not None and slug not in cosine:
            match = re.search(r"cosine=([-0-9.]+)", line)
            cosine[slug] = float(match.group(1)) if match else 0.0
    ranked = [(slug, cosine.get(slug, 0.0)) for slug in order if slug in on_disk]
    passthrough = {slug for slug in order if slug not in on_disk}

    window = max(2 * config.max_lessons, config.max_lessons + 8)
    while True:
        considered, dose = _apply_dosage(active, ranked[:window], config)
        if window >= len(ranked) or len(dose.admitted) >= config.max_lessons:
            break
        window *= 2
    unread = len(ranked) - min(window, len(ranked))

    admitted = {c.slug: c for c in dose.admitted}
    out = [
        line for line in lines
        if (slug := _slug_of_semantic_line(line)) is None or slug in admitted or slug in passthrough
    ]
    listed = set(order)
    for c in dose.admitted:
        if c.slug not in listed:
            out.append(f"{c.slug} | core | importance={c.importance}")
    text = "\n".join(out) + ("\n" if out and (stdout.endswith("\n") or not stdout) else "")
    eligible = [
        slug for slug in order
        if slug in passthrough or (slug in admitted and not admitted[slug].core)
    ]
    dropped = [f"{d.slug} ({d.reason})" for d in dose.dropped]
    if unread:
        dropped.append(f"{unread} more ({dosage.REASON_COUNT})")
    note = ""
    if dropped:
        note = (
            "\n[commontrace] not injected: " + ", ".join(dropped)
            + f"\n[commontrace] budget: {dose.gauge(unread)}\n"
        )
    return text, eligible, note


def _apply_dosage(
    active: list[tuple[str, dict]],
    ranked: list[tuple[str, float]],
    config: retrieval_io.RetrievalConfig,
) -> tuple[dict[str, dict], "dosage.Dose"]:
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
        labels = injection_guard.injection_labels({
            "description": fm.get("description"), "applies_when": fm.get("applies_when"),
            "do_not_apply_when": fm.get("do_not_apply_when"), "body": body,
        })
        if labels:
            print(
                f"[commontrace] quarantined {slug}: injection screen: {', '.join(labels)}",
                file=sys.stderr,
            )
            return
        considered[slug] = {
            "slug": slug, "path": path, "relevance": relevance,
            "core": slug in core_slugs_all, "fm": fm, "body": body,
        }

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
    rate, salt = _effective_holdout(args, root)
    return holdout_io.assign_and_log(
        root, slugs, occasion_id=args.occasion_id, rate=rate, salt=salt,
        relevance=relevance, scorer=scorer, floor=floor,
    )


def _effective_holdout(args: argparse.Namespace, root: str) -> tuple[float, str]:
    config = holdout_io.load_config(root)
    return (
        config.rate if args.holdout_rate is None else args.holdout_rate,
        config.salt if args.experiment_salt is None else args.experiment_salt,
    )


def _slug_of_semantic_line(line: str) -> str | None:
    if line.startswith("#") or "|" not in line:
        return None
    slug = line.split("|", 1)[0].strip()
    return slug or None


def _slugs_from_semantic_output(stdout: str) -> list[str]:
    seen: dict[str, None] = {}
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        if slug is not None:
            seen.setdefault(slug, None)
    return list(seen)


_INDEX_HEADER = re.compile(r"^# Index: \d+ lessons, model=(?P<model>\S+)\s*$", re.MULTILINE)


def _semantic_model_from_output(stdout: str) -> str | None:
    match = _INDEX_HEADER.search(stdout or "")
    return match.group("model") if match else None


def _rerank_depth(
    config: retrieval_io.RetrievalConfig, top_k: int, embedder: str = "", *, candidates: int = 1,
) -> tuple[bool, int, str]:
    if config.rerank == retrieval_io.RERANK_NONE or candidates <= 0:
        return False, top_k, ""
    skipped = rerank_arm.ready(config.rerank)
    if skipped:
        return False, top_k, skipped
    return True, rerank_arm.pool_size(top_k, config.rerank, embedder), ""


def _rerank_pool(
    task: str,
    lessons: list[tuple[str, dict]],
    first_stage: list[tuple[str, float]],
    withdrawn: list[str],
    top_k: int,
    mode: str,
    admit=None,
) -> tuple[list[tuple[str, float]] | None, list[str], str]:
    path_by_slug = {str(fm.get("name", "")): path for path, fm in lessons}
    try:
        page, on_page = rerank_arm.rerank(
            task, [slug for slug, _ in first_stage],
            rerank_arm.texts(
                [slug for slug, _ in first_stage] + list(withdrawn),
                path_by_slug, frontmatter.read,
            ),
            top_k, withdrawn=withdrawn, mode=mode, admit=admit,
        )
    except Exception as exc:  # noqa: BLE001 - a failed rerank serves the first stage
        return None, withdrawn, f"the reranker failed: {type(exc).__name__}: {exc}"
    return page, on_page, ""


def _note_rerank_skipped(config: retrieval_io.RetrievalConfig, why: str, label: str) -> None:
    if config.rerank != retrieval_io.RERANK_NONE and why:
        print(
            f"[commontrace] this store configures rerank={config.rerank!r}, but this query "
            f"kept the first stage's order: {why}. The holdout assignment records "
            f"{label!r} accordingly.",
            file=sys.stderr,
        )


def _run_lexical(args: argparse.Namespace, root: str) -> int:
    lessons, term_cache = lesson_cache.load_active_with_terms(
        root, args.agent_type, reader=lambda p: read_or_warn(frontmatter.read, p),
    )
    lessons = lesson_cache.filter_eligible(
        lessons,
        scope=getattr(args, "scope", ""),
        as_of=getattr(args, "as_of", "") or None,
    )
    lessons = _exclude_shown(lessons, _already_shown(args, root))
    config = retrieval_io.load_config(root)
    floor = config.floor if args.relevance_floor is None else args.relevance_floor
    reliability_lookup, recency_lu = _ranking_adjustments(root, lessons, config)
    harmful = evidence.withdrawn(root, config.harm_policy)
    reranking, depth, rerank_skipped = _rerank_depth(config, args.top_k, candidates=len(lessons))
    ranked = retrieval.rank_lessons(
        args.task, lessons, top_k=depth + len(harmful), floor=floor,
        scorer=config.scorer,
        term_cache=term_cache,
        reliability_lookup=reliability_lookup, reliability_weight=config.reliability_weight,
        recency_lookup=recency_lu, recency_weight=config.recency_weight,
        adaptive_tail=not reranking,
    )
    ranked, withdrawn_ranked = harm.split(ranked, harmful, _core_slugs(lessons), depth)
    withdrawn = [r.slug for r in withdrawn_ranked]
    page = [(r.slug, r.relevance) for r in ranked]
    reranked = None
    if reranking:
        reranked, withdrawn, rerank_skipped = _rerank_pool(
            args.task, lessons, page, withdrawn, args.top_k, config.rerank)
        page = reranked if reranked is not None else page[: args.top_k]
    label = retrieval_io.rerank_label(
        config.scorer,
        config.rerank if reranked is not None else retrieval_io.RERANK_NONE,
    )
    _note_rerank_skipped(config, rerank_skipped, label)
    if config.pinned_for_running_experiment and config.scorer != retrieval.SCORER_IDF:
        print(
            "[commontrace] note: this store has holdout assignments already recorded, so "
            f"retrieval stays on the {config.scorer!r} scorer those assignments were made "
            "under.\n"
            "  Switching scorers changes which lessons are eligible, which would pool two "
            "different treatments\n"
            "  into one comparison. To adopt the adaptive scorer, finish or restart the "
            "experiment:\n"
            "    commontrace retrieval --scorer adaptive-v1 && commontrace experiment --configure "
            "--rate <rate>",
            file=sys.stderr,
        )
    if not ranked:
        if withdrawn:
            _print_withdrawn(withdrawn, harmful)
            return 0
        print(store_state.why_no_results(root, searched="query"))
        return 0

    ranked_by_slug = {r.slug: r for r in ranked}
    considered, dose = _apply_dosage(lessons, page, config)
    if not dose.admitted:
        print(
            f"[commontrace] {len(ranked)} lesson(s) matched, but this store's injection "
            f"budget ({dose.gauge()}) admitted none. Widen it with `commontrace retrieval "
            "--max-lessons`/`--max-chars`. Dropped: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
        _print_withdrawn(withdrawn, harmful)
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
            relevance={c.slug: c.relevance for c in dose.admitted},
            scorer=label,
            floor=floor,
        )

    for c in dose.admitted:
        if c.slug in withheld:
            print(f"{c.slug:45s} [WITHHELD - holdout]")
            continue
        r = ranked_by_slug.get(c.slug)
        if r is not None:
            ce = f" ce={c.relevance:+5.2f}" if reranked is not None else ""
            print(f"{c.slug:45s} rel={r.relevance:4.2f}{ce}  {r.description}")
            print(f"  matched: {', '.join(r.matched_terms)}  ({r.path})")
        else:
            item = considered[c.slug]
            print(f"{c.slug:45s} [core]  {item['fm'].get('description', '')}")
            print(f"  ({item['path']})")

    if dose.dropped:
        print(
            "\n[commontrace] not injected: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
    _print_withdrawn(withdrawn, harmful)
    print(f"[commontrace] budget: {dose.gauge()}")

    if args.experiment:
        print(
            f"\n[commontrace] experiment: {len(dose.admitted) - len(withheld)} injected, "
            f"{len(withheld)} withheld at {_effective_holdout(args, root)[0]:.0%} for occasion "
            f"{args.occasion_id!r}. Record the outcome under that id, then run "
            "`commontrace experiment`."
        )
    return 0


def _semantic_slugs(
    args, root: str, missing_hint: str, extra: int = 0,
) -> tuple[int, list[str], str]:
    script_args = ["--top-k", str(args.top_k + extra)]
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
    config = retrieval_io.load_config(root)
    floor = config.floor if args.relevance_floor is None else args.relevance_floor

    lessons, term_cache = lesson_cache.load_active_with_terms(
        root, args.agent_type, reader=lambda p: read_or_warn(frontmatter.read, p),
    )
    lessons = lesson_cache.filter_eligible(
        lessons,
        scope=getattr(args, "scope", ""),
        as_of=getattr(args, "as_of", "") or None,
    )
    already_shown = _already_shown(args, root)
    lessons = _exclude_shown(lessons, already_shown)
    reliability_lookup, recency_lu = _ranking_adjustments(root, lessons, config)
    harmful = evidence.withdrawn(root, config.harm_policy)
    core = _core_slugs(lessons)
    from commontrace import semantic_arm

    embedder = retrieval_io.embedder_tag(semantic_arm.stored_model(root))
    early, early_depth = None, 0
    if config.rerank != retrieval_io.RERANK_NONE and rerank_arm.available():
        import threading

        loader = threading.Thread(target=rerank_arm.ready, args=(config.rerank,), daemon=True)
        loader.start()
        early_depth = rerank_arm.pool_size(args.top_k, config.rerank, embedder)
        early = _semantic_slugs(args, root, missing_hint, extra=len(harmful) + early_depth - args.top_k)
        loader.join()
    reranking, depth, rerank_skipped = _rerank_depth(config, args.top_k, embedder)
    if config.fusion == retrieval_io.FUSION_GATED and not reranking:
        print(
            "[commontrace] gated fusion needs the reranker, which did not run "
            f"({rerank_skipped or 'rerank is off'}); this query used lexical retrieval.",
            file=sys.stderr,
        )
        return _run_lexical(args, root)
    gated = config.fusion == retrieval_io.FUSION_GATED
    lexical = retrieval.rank_lessons(
        args.task, lessons, top_k=depth + len(harmful), floor=0.0 if gated else floor,
        scorer=config.scorer,
        term_cache=term_cache,
        reliability_lookup=reliability_lookup, reliability_weight=config.reliability_weight,
        recency_lookup=recency_lu, recency_weight=config.recency_weight,
        adaptive_tail=not reranking,
    )
    lexical, withdrawn_lexical = harm.split(lexical, harmful, core, depth)
    floor_cleared = {r.slug for r in lexical + withdrawn_lexical if r.relevance >= floor}

    rc, semantic, stdout = (
        early if early is not None and early_depth == depth
        else _semantic_slugs(args, root, missing_hint, extra=len(harmful) + depth - args.top_k)
    )
    semantic, withdrawn_semantic = harm.split(
        semantic, harmful, core, depth, slug_of=lambda s: s)
    if already_shown and rc == 0:
        core_slugs = {str(fm.get("name", "")) for _p, fm in lessons if dosage.is_core(fm)}
        semantic = [s for s in semantic if s in core_slugs or s not in already_shown]
    if rc != 0:
        sys.stdout.write(stdout)
        print(
            "[commontrace] the semantic arm failed, so this query used lexical "
            "retrieval alone. The holdout assignment records the lexical "
            "configuration, not the fused one -- the two are different "
            "treatments and must not be pooled.",
            file=sys.stderr,
        )
        return _run_lexical(args, root)

    embedder = retrieval_io.embedder_tag(_semantic_model_from_output(stdout))
    if gated:
        fused = [(slug, 0.0) for slug in dict.fromkeys([r.slug for r in lexical] + semantic)]
    else:
        fused = retrieval.reciprocal_rank_fusion(
            {"lexical": [r.slug for r in lexical], "semantic": semantic},
            k=config.rrf_k, top_k=depth,
        )
    withdrawn = list(dict.fromkeys(
        [r.slug for r in withdrawn_lexical] + list(withdrawn_semantic)))
    reranked = None
    if reranking:
        reranked, withdrawn, rerank_skipped = _rerank_pool(
            args.task, lessons, fused, withdrawn, args.top_k, config.rerank,
            admit=rerank_arm.admit_gated(floor_cleared, config.rerank, embedder) if gated else None)
        if reranked is None and gated:
            return _run_lexical(args, root)
        fused = reranked if reranked is not None else fused[: args.top_k]
    label = retrieval_io.rerank_label(
        config.eligibility_label_for(fused=True, embedder=embedder),
        config.rerank if reranked is not None else retrieval_io.RERANK_NONE,
    )
    _note_rerank_skipped(config, rerank_skipped, label)
    if not fused:
        if withdrawn:
            _print_withdrawn(withdrawn, harmful)
            return 0
        print(store_state.why_no_results(root, searched="query"))
        return 0

    by_slug = {r.slug: r for r in lexical}
    described = {
        str(fm.get("name", "")): (str(fm.get("description", "") or ""), path)
        for path, fm in lessons
    }

    _considered, dose = _apply_dosage(lessons, fused, config)
    if not dose.admitted:
        print(
            f"[commontrace] {len(fused)} lesson(s) matched, but this store's injection "
            f"budget ({dose.gauge()}) admitted none. Widen it with `commontrace retrieval "
            "--max-lessons`/`--max-chars`. Dropped: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
        _print_withdrawn(withdrawn, harmful)
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
            relevance={c.slug: c.relevance for c in dose.admitted},
            scorer=label,
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
            (f"ce={fused_score_by_slug[slug]:+5.2f}" if reranked is not None
             else f"rrf={fused_score_by_slug[slug]:5.3f}")
            if slug in fused_score_by_slug
            else "[core]     "
        )
        print(f"{slug:45s} {score_label}  {description}")
        print(f"  arms: {'+'.join(arms) or 'none'}  ({path})")

    if dose.dropped:
        print(
            "\n[commontrace] not injected: "
            + ", ".join(f"{d.slug} ({d.reason})" for d in dose.dropped)
        )
    _print_withdrawn(withdrawn, harmful)
    print(f"[commontrace] budget: {dose.gauge()}")

    if args.experiment:
        print(
            f"\n[commontrace] experiment: {len(dose.admitted) - len(withheld)} injected, "
            f"{len(withheld)} withheld at {_effective_holdout(args, root)[0]:.0%} for occasion "
            f"{args.occasion_id!r}. Record the outcome under that id, then run "
            "`commontrace experiment`."
        )
    return 0


def _fallback_model_args(root: str) -> list[str]:
    logged = retrieval_io.logged_embedding_model(root)
    return ["--fallback-model", logged] if logged else []


def _refresh_stale_index(root: str) -> str:
    reason = _index_is_unusable(root)
    if not reason:
        return ""
    rc, out = run_script(
        root, os.path.join("memory", "attention", "build_index.py"), _fallback_model_args(root),
        "The reference attention scripts ship inside the package.", capture=True,
    )
    after = _index_is_unusable(root)
    if rc == 0 and not after:
        print(f"[commontrace] semantic index was stale ({reason}); refreshed it. "
              f"{out.strip().splitlines()[-1] if out.strip() else ''}".rstrip(),
              file=sys.stderr)
        return ""
    return after or reason


def _index_is_unusable(root: str) -> str:
    index_path = os.path.join(paths.memory_dir(root), "attention", "index.npz")
    try:
        index_mtime = os.path.getmtime(index_path)
    except OSError:
        return "no semantic index has been built yet"

    newest_lesson = 0.0
    newest_name = ""
    try:
        listing = lesson_cache.listing(root)
    except OSError:
        listing = ()
    if listing:
        path, mtime_ns, _size = max(listing, key=lambda entry: entry[1])
        newest_lesson, newest_name = lesson_cache.mtime_seconds(mtime_ns), os.path.basename(path)
    if newest_lesson == 0.0:
        return "no active lessons on disk (a stale index would rank ghosts)"
    if newest_lesson > index_mtime:
        return f"{newest_name} changed after the index was last built"
    return ""


def _has_candidates(args: argparse.Namespace, root: str) -> bool:
    lessons, _terms = lesson_cache.load_active_with_terms(
        root, args.agent_type, reader=lambda p: read_or_warn(frontmatter.read, p))
    lessons = lesson_cache.filter_eligible(
        lessons,
        scope=getattr(args, "scope", ""),
        as_of=getattr(args, "as_of", "") or None,
    )
    return bool(_exclude_shown(lessons, _already_shown(args, root)))


def run(args: argparse.Namespace) -> int:
    with lesson_cache.one_scan():
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    rerank_arm.use_worker()
    as_of = getattr(args, "as_of", "")
    if as_of:
        try:
            lesson_cache.parse_moment(as_of)
        except ValueError as exc:
            print(f"[commontrace] {exc}", file=sys.stderr)
            return 1

    config = retrieval_io.load_config(root)
    if not _has_candidates(args, root):
        return _run_lexical(args, root)
    if (
        not args.lexical
        and config.fusion == retrieval_io.FUSION_NONE
        and config.rerank != retrieval_io.RERANK_NONE
    ):
        return _run_lexical(args, root)

    if not args.lexical and has_attention_deps():
        reason = _refresh_stale_index(root)
        if reason:
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

    if retrieval_io.load_config(root).fusion in (retrieval_io.FUSION_RRF, retrieval_io.FUSION_GATED):
        return _run_hybrid(args, root, missing_hint)

    harmful = evidence.withdrawn(root, retrieval_io.load_config(root).harm_policy)
    script_args = ["--top-k", str(args.top_k + len(harmful))]
    if args.include_importance_floor is not None:
        script_args.extend(["--include-importance-floor", str(args.include_importance_floor)])
    if args.agent_type:
        script_args.extend(["--agent-type", args.agent_type])
    script_args.extend(["--", args.task])
    script_path = os.path.join("memory", "attention", "query.py")

    core: set[str] = set()
    if harmful:
        core = _core_slugs(_iter_active_lessons(
            root, args.agent_type, getattr(args, "scope", ""), getattr(args, "as_of", ""),
        ))
    dosed = not retrieval_io.semantic_only_undosed_pinned(root)

    if not args.experiment:
        rc, stdout = run_script(root, script_path, script_args, missing_hint, capture=True)
        if rc != 0:
            sys.stdout.write(stdout)
            return rc
        stdout, withdrawn = _withdraw_from_semantic(stdout, harmful, core, args.top_k)
        stdout, _eligible, note = _semantic_dose_or_pinned(
            stdout, root, args.agent_type, config, dosed,
            getattr(args, "scope", ""), getattr(args, "as_of", ""),
        )
        sys.stdout.write(stdout + note)
        _print_withdrawn(withdrawn, harmful)
        return 0

    if not args.occasion_id:
        print(
            "[commontrace] --experiment requires --occasion-id: without it the holdout "
            "assignment cannot be joined to an outcome, so nothing could be measured.",
            file=sys.stderr,
        )
        return 1

    rc, stdout = run_script(root, script_path, script_args, missing_hint, capture=True)
    if rc != 0:
        sys.stdout.write(stdout)
        return rc
    stdout, withdrawn = _withdraw_from_semantic(stdout, harmful, core, args.top_k)
    stdout, slugs, note = _semantic_dose_or_pinned(
        stdout, root, args.agent_type, config, dosed,
        getattr(args, "scope", ""), getattr(args, "as_of", ""),
    )
    if not slugs:
        sys.stdout.write(stdout + note)
        _print_withdrawn(withdrawn, harmful)
        print(
            "[commontrace] --experiment: the semantic retriever returned no lessons, "
            "so no holdout arms were recorded for this occasion.",
            file=sys.stderr,
        )
        return 0

    withheld = _apply_holdout(
        args, root, slugs,
        scorer=retrieval_io.semantic_only_label(
            retrieval_io.embedder_tag(_semantic_model_from_output(stdout)), dosed=dosed))
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        if slug is not None and slug in withheld:
            print(f"{slug} | [WITHHELD - holdout]")
        else:
            print(line)
    if note:
        sys.stdout.write(note)
    _print_withdrawn(withdrawn, harmful)
    print(
        f"\n[commontrace] experiment: {len(slugs) - len(withheld)} injected, "
        f"{len(withheld)} withheld at {_effective_holdout(args, root)[0]:.0%} for occasion "
        f"{args.occasion_id!r}. Record the outcome under that id, then run "
        "`commontrace experiment`."
    )
    return 0
