"""The one place a lesson is written, so what it said is never lost.

Every lesson write goes through `write_lesson`. That is not tidiness: a
lesson's content IS the treatment in a randomized experiment
(`commontrace/revision.py`), and a treatment that can change without leaving
a record turns a causal estimate into a number about text nobody can produce
any more.

Before this there were seven `frontmatter.write` call sites for lessons --
`lesson new`, `lesson approve`, `lesson reject`, `distill`, and three on the
MCP surface -- each of which rewrote the file in place with no record of the
previous content. Adding the MCP tools made it worse rather than better: an
agent can now rewrite a lesson mid-experiment, unattended, as an ordinary
part of curating.

THE JOURNAL
-----------
`memory/lesson_revisions.jsonl`, append-only, one line per content change.
Same shape and the same reasons as `memory/holdout_log.jsonl`: written at
the moment of the decision, never reconstructed, and locked because a fleet
writes concurrently by definition.

It records the revision BEFORE and AFTER, who changed it, and why. Three
questions become answerable that were not:

  - Did the treatment hold still while the experiment ran?
    (`integrity.check_treatment_stability`.)
  - What did this lesson say when it produced that effect size?
  - Who activated the instruction the fleet has been following, and when?

Writes that do not change the content are not journaled. `approve` sets
`status`, retrieval bumps `uses` and `last_hit`, a push stamps
`hub_trace_id` -- none of which change one word an agent reads, and
recording them would bury the changes that matter under the ones that do not.
"""

from __future__ import annotations

import datetime
import json
import os
import re

from commontrace import frontmatter, paths, revision

# Letters, digits, underscore and hyphen only. This is the guard that keeps a
# slug from being a path: no separators, no dots, so `../../etc/passwd` never
# resolves to a file. Lives here now that three surfaces resolve slugs (the
# CLI, the MCP server, and the holdout logger); a second copy of a
# path-traversal guard is a second chance to get one of them wrong.
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def canonical_slug(raw: str) -> str:
    """One identity for `foo`, `lesson_foo` and `lesson_foo.md`.

    `foo` and `lesson_foo` resolve to the same file by design, and the
    revision journal keys on whatever `name` happened to be stored (or the
    filename with `.md`). Comparing canonically keeps `history` finding
    records regardless of which form each side used, without renaming any
    stored identity (the stored `name` is the retrieval/experiment identity
    and must not churn mid-store).
    """
    stem = str(raw)
    if stem.endswith(".md"):
        stem = stem[: -len(".md")]
    if stem.startswith("lesson_"):
        stem = stem[len("lesson_"):]
    return stem


def lesson_path(root: str, slug: str) -> str | None:
    """The file for `slug`, or None if there is no such lesson."""
    if not SLUG_RE.match(slug):
        return None
    stem = canonical_slug(slug)
    ldir = paths.lessons_dir(root)
    path = os.path.join(ldir, f"lesson_{stem}.md")
    if os.path.isfile(path):
        return path
    legacy_path = os.path.join(ldir, f"{stem}.md")
    if os.path.isfile(legacy_path):
        return legacy_path
    return None


def revision_for_slug(root: str, slug: str) -> str | None:
    """The revision of `slug` as it stands on disk right now, or None."""
    path = lesson_path(root, slug)
    return current_revision(path) if path else None


def revisions_log_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "lesson_revisions.jsonl")


def current_revision(path: str) -> str | None:
    """The revision of the lesson on disk, or None if it is not readable."""
    try:
        fm, body = frontmatter.read(path)
    except Exception:  # noqa: BLE001 - an unreadable lesson has no revision
        return None
    return revision.revision_of(fm, body)


def write_lesson(
    path: str,
    fm: dict,
    body: str,
    *,
    root: str,
    actor: str = "",
    reason: str = "",
) -> str:
    """Write one lesson, journaling the change if the content moved.

    Returns the revision now on disk. `actor` is who made the change (a
    person, `operator-console`, an agent id); `reason` is free text a later
    reader can use to tell a typo fix from a rewritten rule.

    The journal is appended BEFORE the file is replaced. If the process dies
    between the two, the record says a change was intended that did not
    land -- which is a discrepancy someone can see and resolve. The other
    order loses the fact that anything happened at all, and a silently
    unrecorded rewrite is the exact failure this module exists to prevent.
    """
    before = current_revision(path) if os.path.exists(path) else None
    after = revision.revision_of(fm, body)

    if after != before:
        basename = os.path.basename(path)
        if basename.endswith(".md"):
            basename = basename[: -len(".md")]
        _journal(
            root,
            {
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "lesson": str(fm.get("name") or basename),
                "from": before,
                "to": after,
                "status": fm.get("status"),
                "actor": actor or "unknown",
                "reason": reason,
            },
        )
    frontmatter.write(path, fm, body)
    return after


def _journal(root: str, record: dict) -> None:
    path = revisions_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Locked, flushed and fsynced inside the lock, for the reason
    # holdout_io.assign_and_log spells out: O_APPEND makes one write() atomic
    # but Python buffers, so a fleet writing concurrently can land a flush
    # boundary mid-line. Here a torn line loses the record of what a lesson
    # used to say, which is unrecoverable -- the previous content is already
    # gone from the file.
    with frontmatter.locked(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def read_revisions(root: str) -> tuple[list[dict], int]:
    """Every recorded content change, plus a count of unparseable lines."""
    path = revisions_log_path(root)
    if not os.path.isfile(path):
        return [], 0
    out: list[dict] = []
    corrupt = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                corrupt += 1
                continue
            if isinstance(record, dict) and record.get("lesson"):
                out.append(record)
            else:
                corrupt += 1
    return out, corrupt


def history(root: str, slug: str) -> list[dict]:
    """One lesson's content changes, oldest first.

    Matches canonically, so `history foo` finds records journaled as
    `lesson_foo` (or `lesson_foo.md` via the filename fallback in
    write_lesson) and vice versa.
    """
    records, _ = read_revisions(root)
    want = canonical_slug(slug)
    return [r for r in records if canonical_slug(str(r.get("lesson") or "")) == want]
