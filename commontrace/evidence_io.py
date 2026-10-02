from __future__ import annotations

import glob
import os

from commontrace import frontmatter, holdout_io, paths, reliability, templates, trace_io
from commontrace.commands._format import read_or_warn

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
                occasion_id=str(inst.get("id", "")),
                retrieved=retrieved,
                hit=list(ext.get("lessons_hit") or []),
                succeeded=succeeded if isinstance(succeeded, bool) else None,
            )
        )

    return ev


def uncaptured_retrieval_counts(root: str) -> dict[str, int]:
    captured_occasions = {e.occasion_id for e in load_evidence(root)}
    records, _corrupt = holdout_io.read_log(root)
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for r in records:
        if not r.injected or r.occasion_id in captured_occasions:
            continue
        key = (r.lesson, r.occasion_id)
        if key in seen:
            continue
        seen.add(key)
        counts[r.lesson] = counts.get(r.lesson, 0) + 1
    return counts


def reliability_snapshot(root: str) -> dict[str, float]:
    evidence = load_evidence(root)
    if not evidence:
        return {}
    scores = reliability.score_lessons(evidence)
    return reliability.ranking_adjustments(scores)
