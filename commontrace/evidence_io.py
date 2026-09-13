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

from commontrace import frontmatter, paths, reliability, templates, trace_io
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
    """Collect occasions on which lessons were injected, from both shapes the
    protocol supports.

    Episodes (the code-review profile) carry retrieval + hit + verdict
    directly. Generic traces carry outcomes but not, today, which lessons
    were injected -- so they contribute outcome evidence only where a
    profile has recorded retrievals in `extensions`. That asymmetry is real
    and is surfaced to the user rather than hidden, because it determines
    whether this report can say anything at all.
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
