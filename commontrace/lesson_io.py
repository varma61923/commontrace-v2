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
    # Read once, not via current_revision(): that helper discards the actual
    # frontmatter/body after hashing them, and the journal entry below needs
    # the text itself, not just its hash, to answer "what did this lesson
    # say on date X" later (see content_as_of).
    before_fm: dict | None = None
    before_body: str | None = None
    before: str | None = None
    if os.path.exists(path):
        try:
            before_fm, before_body = frontmatter.read(path)
            before = revision.revision_of(before_fm, before_body)
        except Exception:  # noqa: BLE001 - an unreadable prior file has no revision
            before = None
    after = revision.revision_of(fm, body)

    if after != before:
        basename = os.path.basename(path)
        if basename.endswith(".md"):
            basename = basename[: -len(".md")]
        record = {
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "lesson": str(fm.get("name") or basename),
            "from": before,
            "to": after,
            "status": fm.get("status"),
            "actor": actor or "unknown",
            "reason": reason,
        }
        # Only when there WAS a prior version: a lesson's first write has
        # nothing to reconstruct before it, and `before is None` already
        # says so via `record["from"]`. Kept out of the record entirely
        # (not written as null) so an old reader that predates this field
        # sees the exact same shape it always has for a creation event.
        if before_fm is not None and before_body is not None:
            record["before_frontmatter"] = before_fm
            record["before_body"] = before_body
        _journal(root, record)
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


def _parse_at(value: str) -> datetime.datetime | None:
    """Parse a journal `at` timestamp or a caller-supplied point in time,
    tolerant of a bare date ("2026-07-01") the way a human would type one
    at a command line. None on anything unparseable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


class ContentAsOfError(Exception):
    """`content_as_of` could not answer, and why -- distinct from a plain
    None so a caller (and `lesson history --as-of`) can print the actual
    reason rather than a generic "nothing found"."""


def content_as_of(root: str, slug: str, at: str) -> tuple[dict, str]:
    """What `slug` said at the point in time `at`, reconstructed from the
    revision journal.

    Before this, `memory/lesson_revisions.jsonl` recorded a before/after
    HASH on every change -- enough to detect that a lesson changed
    mid-experiment (`integrity.check_treatment_stability`), not enough to
    answer what it actually said on a given date, because the prior text
    itself was never kept. `write_lesson` now stashes the full prior
    frontmatter/body in `before_frontmatter`/`before_body` on every entry
    that has one; this walks that chain to find the version that was live
    at `at`.

    Raises `ContentAsOfError` (never returns None) for every way this can
    fail to answer, each with a distinct, actionable message: no such
    lesson, an unparseable `at`, or content that predates what this
    journal can reconstruct (either the store adopted this field after the
    lesson's own history began, or `at` is before the lesson existed at
    all).
    """
    target = _parse_at(at)
    if target is None:
        raise ContentAsOfError(
            f"could not parse {at!r} as a date/time -- use YYYY-MM-DD or full ISO 8601."
        )

    records = history(root, slug)
    for record in records:
        record_at = _parse_at(str(record.get("at") or ""))
        if record_at is not None and record_at > target:
            before_fm = record.get("before_frontmatter")
            before_body = record.get("before_body")
            if isinstance(before_fm, dict) and isinstance(before_body, str):
                return before_fm, before_body
            raise ContentAsOfError(
                f"'{slug}' changed at {record.get('at')} (after {at}), but that entry "
                "predates this journal recording full content, not just a hash -- "
                "the text active at that point cannot be reconstructed from here."
            )

    # No change happened after `at`: either the current file is what was
    # live then, or the lesson has never been journaled at all (written
    # before this module existed, or a schema-only touch with no content
    # change since) and the file on disk is simply the only version there
    # has ever been.
    path = lesson_path(root, slug)
    if path is None:
        raise ContentAsOfError(f"no lesson found for slug '{slug}'.")
    try:
        return frontmatter.read(path)
    except Exception as exc:  # noqa: BLE001 - surfaced as this function's own error
        raise ContentAsOfError(f"could not read '{slug}': {exc}") from exc
