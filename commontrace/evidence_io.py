"""Shared loader for lesson-retrieval evidence: which lessons were injected
into which decisions, and how those decisions turned out.

Extracted from commontrace/commands/reliability_cmd.py so `commontrace
reliability`, `commontrace impact` and `commontrace pilot` read the exact
same evidence set. Duplicating this across commands is how two "lessons
reused" numbers would quietly drift apart -- the same failure mode
commontrace/_lexical.py was created to close for tokenization.
"""
from __future__ import annotations

import glob
import os

from commontrace import frontmatter, holdout_io, paths, reliability, templates, trace_io
from commontrace.commands._format import read_or_warn

# Re-exported: the body is stashed on the returned frontmatter dict under
# this key so a caller holding only that dict can still see the whole lesson
# -- concretely templates.unfilled_placeholders, which has to read the
# "## Rule" section to tell a written lesson from unedited scaffolding.
# Returned on the same dict rather than as a second parallel list so every
# existing caller (reliability, taxonomy, pilot) keeps its current shape.
BODY_KEY = templates.BODY_KEY


def load_active_lessons(root: str, status: str = "active") -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        result = read_or_warn(frontmatter.read, path)
        if result is None:
            continue
        fm, body = result
        lesson_status = fm.get("status") or "active"
        if status is not None and lesson_status != status:
            continue
        fm[BODY_KEY] = body
        out.append(fm)
    return out


def load_evidence(root: str, traces: list[dict] | None = None) -> list[reliability.Evidence]:
    """Collect occasions on which lessons were injected, from every shape
    the protocol supports.

    Episodes (the code-review profile) carry retrieval + hit + verdict
    directly. Generic traces carry outcomes but not, today, which lessons
    were injected -- so they contribute outcome evidence only where a
    profile has recorded retrievals in `extensions`. That asymmetry is real
    and is surfaced to the user rather than hidden, because it determines
    whether this report can say anything at all.

    Deliberately does NOT also read the holdout log for occasions neither
    of the above covers (a `--experiment` retrieval whose agent never
    called `capture` afterward) -- see `uncaptured_retrieval_counts` for
    that data instead, and its own docstring for why folding it in HERE
    would be actively wrong: `score_lessons` reads a missing slug in
    `hit` as "retrieved and did not help," so an occasion with no captured
    outcome at all would score as a confirmed miss rather than as the
    unknown it actually is, dragging every such lesson's measured
    precision down for no reason but under-reporting.
    """
    ev: list[reliability.Evidence] = []

    for path in sorted(glob.glob(os.path.join(paths.episodes_dir(root), "*.md"))):
        if os.path.basename(path).startswith("_") or "template" in os.path.basename(path):
            continue
        result = read_or_warn(frontmatter.read, path)
        if result is None:
            continue
        fm, _ = result
        retrieved = list(fm.get("lessons_retrieved_by_alpha") or [])
        if not retrieved:
            continue
        # Mapping a code-review verdict onto a binary "did the task succeed"
        # is a modeling choice, not a fact, so it is stated rather than
        # buried: CONFORM is success, ABANDON is failure, and ARBITRATION
        # (the A/B loop failed to converge and the orchestrator had to
        # decide) is counted as failure because the pipeline did not resolve
        # it on its own. That is defensible but debatable -- ARBITRATION is a
        # degraded outcome rather than an outright loss. Anything else maps
        # to None and is excluded from lift entirely rather than guessed at.
        verdict = str(fm.get("verdict", "")).upper()
        succeeded = True if verdict == "CONFORM" else (False if verdict in ("ABANDON", "ARBITRATION") else None)
        ev.append(
            reliability.Evidence(
                occasion_id=str(fm.get("name", os.path.basename(path))),
                retrieved=retrieved,
                hit=list(fm.get("lessons_hit") or []),
                succeeded=succeeded,
            )
        )

    if traces is None:
        trace_instances = []
        for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
            if os.path.basename(path) == "README.md":
                continue
            result = read_or_warn(trace_io.read, path)
            if result is None:
                continue
            inst, _ = result
            trace_instances.append(inst)
    else:
        trace_instances = traces

    for inst in trace_instances:
        ext = inst.get("extensions") or {}
        retrieved = list(ext.get("lessons_retrieved") or [])
        if not retrieved:
            continue
        outcome = inst.get("outcome") or {}
        succeeded = outcome.get("resolved")
        if outcome.get("repeated_error") is True:
            succeeded = False
        ev.append(
            reliability.Evidence(
                # Full trace id: truncating (e.g. [:12]) collides distinct
                # prefixed ids like JIRA-...-4711 vs ...-4712 (cf. capture
                # --occasion-id warning) and merges occasions in lift/verdicts.
                occasion_id=str(inst.get("id", "")),
                retrieved=retrieved,
                hit=list(ext.get("lessons_hit") or []),
                succeeded=succeeded if isinstance(succeeded, bool) else None,
            )
        )

    return ev


def uncaptured_retrieval_counts(root: str) -> dict[str, int]:
    """Per lesson slug, how many occasions the holdout log shows it was
    actually injected into that no episode or trace ever recorded an
    outcome for -- an agent that retrieved under `--experiment` and never
    called `capture` afterward.

    A coverage signal, not scoring input: this is deliberately NOT folded
    into `load_evidence`/`reliability.score_lessons`. That function reads a
    slug absent from `Evidence.hit` as "retrieved and did not help," so an
    uncaptured occasion (outcome genuinely UNKNOWN, not a confirmed miss)
    would score as a miss and drag the lesson's measured precision down for
    no reason but under-reporting -- exactly the kind of guessed-at outcome
    this codebase's importers (`commontrace/adapters.py`) refuse to invent
    elsewhere. `commontrace reliability` prints this alongside each
    lesson's verdict instead, as "N more retrieval(s) with no captured
    outcome" -- a prompt to capture more, not a number the verdict itself
    should absorb.

    Excludes any occasion an episode or trace DOES cover, so a captured
    occasion is never counted here as if it were still missing -- that
    would silently claim under-reporting for an occasion whose outcome the
    real record already has, whatever it turned out to be (including a
    genuine, recorded miss).

    Only `injected=True` rows count: a lesson the holdout withheld was
    matched but deliberately never shown, so counting it would credit a
    lesson with an occasion it was never actually part of -- the same
    reasoning `query_cmd.py`'s own `eligible` list excludes a
    budget-dropped lesson from randomization for.
    """
    captured_occasions = {e.occasion_id for e in load_evidence(root)}
    records, _corrupt = holdout_io.read_log(root)
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for r in records:
        if not r.injected or r.occasion_id in captured_occasions:
            continue
        # One occasion assigning the same lesson twice (a retry, or two
        # calls under one occasion_id) is still one occasion's worth of
        # missing evidence for that lesson, not two.
        key = (r.lesson, r.occasion_id)
        if key in seen:
            continue
        seen.add(key)
        counts[r.lesson] = counts.get(r.lesson, 0) + 1
    return counts


def reliability_snapshot(root: str) -> dict[str, float]:
    """This store's current reliability verdicts, as a ranking adjustment per
    slug -- see `commontrace/reliability.py`'s `ranking_adjustments`.

    Recomputed fresh on every call rather than cached to disk: evidence is
    file-based (episodes/traces already on disk) and this product's own
    corpora are hundreds to low-thousands of occasions, not the scale where
    re-globbing and re-scoring costs anything a single retrieval call would
    notice. A store with no evidence at all (a brand-new fleet) returns an
    empty dict, which `retrieval.rank_lessons` reads as "no adjustment" for
    every lesson -- the same as UNPROVEN.

    This is the "precomputed reliability snapshot" `retrieval_io.py`'s
    `reliability_weight` docstring refers to: computed once per retrieval
    call, not once per candidate lesson inside the ranking loop.
    """
    evidence = load_evidence(root)
    if not evidence:
        return {}
    scores = reliability.score_lessons(evidence)
    return reliability.ranking_adjustments(scores)
