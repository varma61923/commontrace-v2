"""Read-only views behind the Learning Ledger console: value, releases, design, forensics, digest.

Every number here is computed from the store's own records; nothing is
estimated beyond what the experiment supports, and every view says what it is
not. All functions are pure reads.
"""
from __future__ import annotations

import datetime
import difflib
import json
import os

from commontrace import experiment, frontmatter, holdout_io, lesson_io, paths, release

CHARS_PER_TOKEN = 4


def _tokens(text: str) -> int:
    return max(1, -(-len(text) // CHARS_PER_TOKEN))


def _lesson_tokens(root: str, slug: str) -> int | None:
    path = lesson_io.lesson_path(root, slug)
    if path is None:
        return None
    _fm, body = frontmatter.read(path)
    return _tokens(body)


def executive(root: str, effects: list, *, harmful: set[str], value_per_occasion: float | None = None) -> dict:
    """Proven value, harm withdrawn and lift per injected token, from the anytime-valid effects.

    "Proven" counts only HELPS memories, at the conservative lower bound:
    ``ci_low x injected occasions``. Lift per token divides each memory's effect
    by the tokens it costs per delivery; the frontier adds memories best lift per
    token first, which is the order to keep when context is scarce.
    """
    rows, improved = [], 0.0
    for e in effects:
        tokens = _lesson_tokens(root, e.lesson_slug)
        row = {"memory": e.lesson_slug, "verdict": e.verdict, "effect": e.effect, "ci": [e.ci_low, e.ci_high],
               "n": [e.n_injected, e.n_withheld], "tokens": tokens, "withdrawn": e.lesson_slug in harmful,
               "lift_per_1k_tokens": round(e.effect / tokens * 1000, 4) if tokens else None}
        if e.verdict == experiment.VERDICT_HELPS:
            row["occasions_improved_low"] = round(max(0.0, e.ci_low) * e.n_injected, 1)
            improved += row["occasions_improved_low"]
        if e.verdict == experiment.VERDICT_HURTS:
            # At least this many failures per 1,000 deliveries, by the conservative bound.
            row["failures_avoided_per_1k_if_withdrawn"] = round(-e.ci_high * 1000, 1)
        rows.append(row)
    frontier, spent, lift = [], 0, 0.0
    useful = [r for r in rows if r["verdict"] == experiment.VERDICT_HELPS and r["tokens"]]
    for r in sorted(useful, key=lambda r: -r["lift_per_1k_tokens"]):
        spent += r["tokens"]
        lift += r["effect"]
        frontier.append({"memory": r["memory"], "cumulative_tokens": spent, "cumulative_effect": round(lift, 4)})
    return {
        "memories": rows, "frontier": frontier,
        "proven_occasions_improved": round(improved, 1),
        "proven_value": round(improved * value_per_occasion, 2) if value_per_occasion else None,
        "value_per_occasion": value_per_occasion,
        "harmful": sum(r["verdict"] == experiment.VERDICT_HURTS for r in rows),
        "harmful_withdrawn": sum(r["verdict"] == experiment.VERDICT_HURTS and r["withdrawn"] for r in rows),
        "basis": "HELPS memories at the anytime-valid lower bound; never point estimates",
    }


def _text_at(root: str, slug: str, rev: str) -> str | None:
    path = lesson_io.lesson_path(root, slug)
    if path is not None and lesson_io.current_revision(path) == rev:
        return frontmatter.read(path)[1]
    rows, _ = lesson_io.read_revisions(root)
    for row in rows:
        if lesson_io.canonical_slug(str(row.get("lesson", ""))) == lesson_io.canonical_slug(slug) \
                and row.get("from") == rev and "before_body" in row:
            return row["before_body"]
    return None


def releases(root: str) -> list[dict]:
    return [{"id": r.release_id, "parent": r.parent_id, "created_at": r.created_at, "actor": r.actor,
             "reason": r.reason, "lessons": len(r.entries)} for r in reversed(release.read_all(root))]


def release_diff(root: str, to_id: str, from_id: str | None = None) -> dict:
    """What `to_id` changed against `from_id` (default: its parent), with text diffs where recoverable."""
    after = release.find(root, to_id)
    if after is None:
        raise ValueError("unknown release")
    before = release.find(root, from_id or after.parent_id) if (from_id or after.parent_id) else None
    d = release.diff(before, after)
    changed = []
    for slug, old, new in d.changed:
        a, b = _text_at(root, slug, old), _text_at(root, slug, new)
        text = ("\n".join(difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(),
                                               f"{slug}@{old[:8]}", f"{slug}@{new[:8]}", lineterm=""))
                if a is not None and b is not None else None)
        changed.append({"lesson": slug, "from": old, "to": new, "diff": text})
    return {"from": before.release_id if before else None, "to": after.release_id,
            "added": [e.slug for e in d.added], "removed": [e.slug for e in d.removed], "changed": changed}


def design(*, baseline: float, effect: float, rate: float, power: float = 0.8,
           daily: float | None = None, budget: int | None = None) -> dict:
    plan = experiment.plan(effect=effect, baseline=baseline, rate=rate, power=power, occasions_budget=budget)
    out = {"baseline": baseline, "effect": effect, "rate": rate, "power": power,
           "n_per_arm": plan.n_per_arm, "occasions_needed": plan.occasions_needed,
           "rate_for_budget": plan.rate_for_budget, "verdict": plan.verdict,
           "command": f"commontrace experiment --configure --rate {rate:g}"}
    if daily:
        out["days_needed"] = round(plan.occasions_needed / daily, 1)
    return out


def forensics(root: str, occasion: str) -> dict:
    """Everything recorded about one occasion: assignments, outcome, recall receipt, incident reports."""
    records, _corrupt = holdout_io.read_log(root)
    assignments = [{"memory": r.lesson, "injected": r.injected, "rank": r.rank, "revision": r.revision,
                    "relevance": r.relevance, "at": r.at.isoformat() if r.at else None}
                   for r in records if r.occasion_id == occasion]
    outcome = holdout_io.read_outcomes(root).get(occasion)

    def journal(name: str) -> list[dict]:
        path = os.path.join(paths.memory_dir(root), name + ".jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [{k: v for k, v in r.items() if k != "origin"} | {"signed": bool(r.get("origin"))}
                for r in rows if r.get("id") == occasion]

    return {"occasion": occasion, "assignments": assignments, "outcome": outcome,
            "recall_receipts": journal("recall-receipts"), "incident_reports": journal("incident-reports"),
            "found": bool(assignments or outcome is not None)}


def digest(root: str, effects: list, *, days: int = 7, now: datetime.datetime | None = None) -> dict:
    """A Markdown summary of what the store learned and measured in the last `days`."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=days)

    def recent(when) -> bool:
        if isinstance(when, datetime.datetime):
            moment = when
        else:
            try:
                moment = datetime.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            except ValueError:
                return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment >= since

    records, _ = holdout_io.read_log(root)
    window = [r for r in records if recent(r.at)]
    revisions, _ = lesson_io.read_revisions(root)
    changes = [r for r in revisions if recent(r.get("at"))]
    approved = sorted({r["lesson"] for r in changes if r.get("status") == "active"})
    drafted = sorted({r["lesson"] for r in changes if r.get("status") == "review"})
    cut = [r for r in release.read_all(root) if recent(r.created_at)]
    helps = [e for e in effects if e.verdict == experiment.VERDICT_HELPS]
    hurts = [e for e in effects if e.verdict == experiment.VERDICT_HURTS]
    lines = [f"# CommonTrace digest: {since.date()} to {now.date()}", "",
             f"- Occasions randomized this period: {len({r.occasion_id for r in window})} "
             f"({sum(r.injected for r in window)} deliveries, {sum(not r.injected for r in window)} withheld)",
             f"- Memories proven to help: {len(helps)}; proven to hurt: {len(hurts)}",
             f"- Lessons approved: {len(approved)}; drafted for review: {len(drafted)}; releases cut: {len(cut)}", ""]
    if helps:
        lines += ["## Proven helpful", *[f"- {e.lesson_slug}: {e.effect:+.1%} [{e.ci_low:+.1%}, {e.ci_high:+.1%}]"
                                        for e in sorted(helps, key=lambda e: -e.ci_low)], ""]
    if hurts:
        lines += ["## Proven harmful", *[f"- {e.lesson_slug}: {e.effect:+.1%} [{e.ci_low:+.1%}, {e.ci_high:+.1%}]"
                                        for e in sorted(hurts, key=lambda e: e.ci_high)], ""]
    if approved or drafted:
        lines += ["## Lesson changes", *[f"- approved: {s}" for s in approved], *[f"- in review: {s}" for s in drafted],
                  ""]
    lines.append("Verdicts are anytime-valid randomized holdout readings; nothing here is a point-estimate claim.")
    return {"since": since.isoformat(timespec="seconds"), "until": now.isoformat(timespec="seconds"),
            "markdown": "\n".join(lines) + "\n",
            "counts": {"occasions": len({r.occasion_id for r in window}), "helps": len(helps), "hurts": len(hurts),
                       "approved": len(approved), "drafted": len(drafted), "releases": len(cut)}}
