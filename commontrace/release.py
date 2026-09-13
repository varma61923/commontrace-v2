"""What the fleet was running, as one named, immutable thing.

WHY THIS EXISTS
---------------
This product had two of the three identities a deployable change needs and
was missing the third:

    lesson      a slug. Mutable: the same name, different text, over time.
    revision    commontrace/revision.py -- content identity for ONE lesson,
                so an experiment can say which text it measured.
    release     absent.

A revision answers "what did lesson X say?". Nothing answered "what was the
fleet running?", and that is the question every operational conversation
turns out to be about:

  * **Rollback.** Something started going wrong on Tuesday. The remedy is
    "put back what we had on Monday", and there was no Monday to put back --
    only N lesson files, each of which would have to be reverted by hand, in
    an order nobody recorded, with no way to tell when you were done.
  * **Attribution.** An effect appeared after a batch of six lessons was
    approved over an afternoon. Which six? `status: active` is a property of
    each file NOW; it carries no memory of when the set changed or what it
    changed from.
  * **Atomicity.** Approving six lessons one at a time means the fleet runs
    five intermediate combinations nobody chose and nobody measured. If the
    fourth approval is the one that breaks something, the three states in
    between are not reproducible.

A Release is the missing identity: an immutable, content-addressed snapshot
of exactly which (lesson, revision) pairs were active together, what it
replaced, who cut it and why. It is a POINTER TO EXISTING CONTENT -- it
copies no lesson text and can never disagree with the lessons themselves,
because it stores their revisions rather than their words.

STALE-BASE REJECTION, AND WHY IT IS NOT OPTIONAL
------------------------------------------------
A fleet has several curators and, now, agents that can approve. Two of them
reading the same active set, each approving a different lesson, each cutting
a release from what they saw, is the ordinary lost-update problem -- and the
thing lost is not a text edit but a deployment decision. `cut` therefore
takes the release the caller believed it was building on and refuses if the
store has moved since, which is the same discipline ACE and agent-knowledge
apply to candidate promotion and the same one `frontmatter.locked` already
applies one level down.

WHAT THIS DOES NOT DO
---------------------
It does not gate retrieval. Retrieval reads the lessons' own `status`, as it
always has, and a release RECORDS that set rather than deciding it -- so a
store that never cuts a release behaves exactly as it does today, and a
release can never make retrieval and the audit log disagree about what was
active.

Making the release the thing retrieval resolves through (so a fleet can run
an older set without editing files, and so canary/ring targeting has
something to target) is the next step and deliberately not this one: it
changes what every agent reads, and that belongs behind its own decision
rather than arriving as a side effect of adding an audit trail.
"""

from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
from dataclasses import dataclass, field

from commontrace import frontmatter, lesson_io, paths, revision

_FIELD_SEP = "\x1f"
_RELEASE_GENESIS = hashlib.sha256(b"commontrace-release-v1").hexdigest()

RELEASES_FILENAME = "releases.jsonl"


class ReleaseError(RuntimeError):
    """A release that cannot be cut or cannot be read."""


class StaleBaseError(ReleaseError):
    """The store moved between reading it and cutting a release from it.

    Carries both ids so the caller can show the difference rather than only
    refusing: a lost deployment decision is worth more explanation than a
    conflict message.
    """

    def __init__(self, message: str, expected: str, actual: str):
        super().__init__(message)
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class Entry:
    """One lesson, pinned to the exact text that was active."""

    slug: str
    revision: str


@dataclass(frozen=True)
class Release:
    release_id: str
    parent_id: str
    entries: tuple[Entry, ...]
    created_at: str
    actor: str
    reason: str

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(e.slug for e in self.entries)

    def revision_of(self, slug: str) -> str | None:
        for entry in self.entries:
            if entry.slug == slug:
                return entry.revision
        return None

    def to_dict(self) -> dict:
        return {
            "release_id": self.release_id,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "actor": self.actor,
            "reason": self.reason,
            "entries": [{"slug": e.slug, "revision": e.revision} for e in self.entries],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Release:
        if not isinstance(raw, dict):
            raise ReleaseError(f"expected an object, got {type(raw).__name__}")
        entries = tuple(
            Entry(slug=str(e.get("slug", "")), revision=str(e.get("revision", "")))
            for e in (raw.get("entries") or [])
            if isinstance(e, dict)
        )
        return cls(
            release_id=str(raw.get("release_id", "")),
            parent_id=str(raw.get("parent_id", "")),
            entries=entries,
            created_at=str(raw.get("created_at", "")),
            actor=str(raw.get("actor", "")),
            reason=str(raw.get("reason", "")),
        )


def releases_log_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), RELEASES_FILENAME)


def compute_id(parent_id: str, entries: tuple[Entry, ...]) -> str:
    """Content-addressed, over the parent and the pinned set.

    Including the parent makes the id a position in a history rather than a
    description of a set -- so cutting the same set twice, from different
    bases, produces two different releases, which is correct: they are
    different deployments that happen to agree about the outcome.

    Sorted by slug and fixed-separated for the same reason the value
    ledger's rows are: reproducible by anyone holding the printed contents.
    """
    rows = _FIELD_SEP.join(
        f"{e.slug}={e.revision}" for e in sorted(entries, key=lambda e: e.slug)
    )
    return hashlib.sha256(
        (parent_id + _FIELD_SEP + rows).encode("utf-8")
    ).hexdigest()


def active_entries(root: str) -> tuple[Entry, ...]:
    """Every active lesson in the store, pinned to its current revision.

    Reads the lessons themselves rather than any cached index: a release
    records what IS active, and a stale index would let it record something
    that was not.
    """
    entries: list[Entry] = []
    pattern = os.path.join(paths.lessons_dir(root), "*.md")
    for path in sorted(glob.glob(pattern)):
        try:
            fm, body = frontmatter.read(path)
        except (frontmatter.FrontmatterError, OSError, UnicodeDecodeError):
            # An unreadable lesson is not silently omitted from a release --
            # that would record a set the fleet is not running. Named, so
            # whoever cuts the release can fix it first.
            raise ReleaseError(
                f"cannot read {path}, so the active set cannot be determined. "
                "A release that quietly skipped it would claim the fleet is running "
                "something it is not."
            ) from None
        if (fm.get("status") or "active") != "active":
            continue
        slug = str(fm.get("name") or os.path.basename(path)[: -len(".md")])
        entries.append(Entry(slug=slug, revision=revision.revision_of(fm, body)))
    return tuple(sorted(entries, key=lambda e: e.slug))


def read_all(root: str) -> list[Release]:
    """Every release ever cut, oldest first."""
    path = releases_log_path(root)
    if not os.path.isfile(path):
        return []
    out: list[Release] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # A corrupt line is skipped rather than fatal, matching
                # lesson_io.read_revisions: a damaged history is still worth
                # more than no history, and the gap is visible in the chain.
                continue
            try:
                out.append(Release.from_dict(record))
            except ReleaseError:
                continue
    return out


def current(root: str) -> Release | None:
    """The most recent release, or None if none has been cut."""
    releases = read_all(root)
    return releases[-1] if releases else None


def current_id(root: str) -> str:
    """The id to pass back as `base_id`. The genesis when nothing has been
    cut, so a first release has a real parent rather than an empty string
    that could be spliced onto any other chain."""
    latest = current(root)
    return latest.release_id if latest else _RELEASE_GENESIS


def cut(
    root: str,
    *,
    base_id: str,
    actor: str = "",
    reason: str = "",
    now: datetime.datetime | None = None,
) -> Release:
    """Record the active set as an immutable release.

    `base_id` is the release the caller believed it was building on --
    `current_id(root)`, read before the change it is now recording. If the
    store has moved since, this raises `StaleBaseError` rather than
    overwriting somebody else's deployment decision.
    """
    latest = current_id(root)
    if base_id != latest:
        raise StaleBaseError(
            f"refusing to cut a release from {base_id[:12]}: the store is at "
            f"{latest[:12]}. Somebody else changed the active set since this was "
            "read. Re-read the current release, confirm the combined set is what "
            "you intend to run, and cut again -- a release recorded over theirs "
            "would lose a deployment decision, not a text edit.",
            expected=base_id, actual=latest,
        )

    entries = active_entries(root)
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    release = Release(
        release_id=compute_id(base_id, entries),
        parent_id=base_id,
        entries=entries,
        created_at=moment.isoformat(),
        actor=actor or "unknown",
        reason=reason,
    )

    path = releases_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(release.to_dict(), ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return release


def find(root: str, release_id: str) -> Release | None:
    """By full id or unambiguous prefix. Returns None if nothing matches;
    raises if a prefix matches more than one, because acting on the wrong
    release is worse than being asked to type more."""
    releases = read_all(root)
    exact = [r for r in releases if r.release_id == release_id]
    if exact:
        return exact[-1]
    partial = [r for r in releases if r.release_id.startswith(release_id)]
    if len(partial) > 1:
        raise ReleaseError(
            f"{release_id!r} matches {len(partial)} releases "
            f"({', '.join(r.release_id[:12] for r in partial[:4])}...). "
            "Use more characters."
        )
    return partial[0] if partial else None


@dataclass(frozen=True)
class Diff:
    """What changed between two releases."""

    added: tuple[Entry, ...] = field(default_factory=tuple)
    removed: tuple[Entry, ...] = field(default_factory=tuple)
    changed: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def summary(self) -> str:
        if self.empty:
            return "no change"
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.changed:
            parts.append(f"{len(self.changed)} rewritten")
        return ", ".join(parts)


def diff(before: Release | None, after: Release) -> Diff:
    """What `after` changed relative to `before`.

    A REWRITTEN lesson (same slug, different revision) is its own category
    rather than a remove plus an add: "we changed what this rule says" and
    "we swapped one rule for another" are different deployments, and a diff
    that renders them identically is the one an operator misreads at 3am.
    """
    old = {e.slug: e.revision for e in (before.entries if before else ())}
    new = {e.slug: e.revision for e in after.entries}

    added = tuple(
        Entry(slug, new[slug]) for slug in sorted(new.keys() - old.keys())
    )
    removed = tuple(
        Entry(slug, old[slug]) for slug in sorted(old.keys() - new.keys())
    )
    changed = tuple(
        (slug, old[slug], new[slug])
        for slug in sorted(old.keys() & new.keys())
        if old[slug] != new[slug]
    )
    return Diff(added=added, removed=removed, changed=changed)


@dataclass(frozen=True)
class RollbackPlan:
    """What returning to a release would do, worked out before doing it."""

    target: Release
    deactivate: tuple[str, ...] = field(default_factory=tuple)
    reactivate: tuple[str, ...] = field(default_factory=tuple)
    #: Active in the target at a revision the store no longer holds. These
    #: CANNOT be restored by flipping a status -- the text is gone -- so a
    #: rollback that silently reactivated the current text would put back
    #: something that was never in the target release.
    unrestorable: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return not self.unrestorable

    @property
    def empty(self) -> bool:
        return not (self.deactivate or self.reactivate or self.unrestorable)


def plan_rollback(root: str, target: Release) -> RollbackPlan:
    """Work out what returning to `target` involves, without doing it.

    Separated from the doing because the interesting cases are the ones a
    caller should see first: a lesson whose text has been rewritten since
    cannot be restored by changing its status, and a rollback that pretended
    otherwise would put back a different rule under the same name.
    """
    live = {e.slug: e.revision for e in active_entries(root)}
    wanted = {e.slug: e.revision for e in target.entries}

    deactivate = tuple(sorted(live.keys() - wanted.keys()))
    reactivate: list[str] = []
    unrestorable: list[tuple[str, str]] = []
    for slug, wanted_revision in sorted(wanted.items()):
        if slug in live:
            if live[slug] != wanted_revision:
                unrestorable.append((slug, wanted_revision))
            continue
        path = lesson_io.lesson_path(root, slug)
        if path is None or lesson_io.current_revision(path) != wanted_revision:
            unrestorable.append((slug, wanted_revision))
            continue
        reactivate.append(slug)

    return RollbackPlan(
        target=target,
        deactivate=deactivate,
        reactivate=tuple(reactivate),
        unrestorable=tuple(unrestorable),
    )


def apply_rollback(
    root: str, plan: RollbackPlan, *, actor: str = "", allow_partial: bool = False
) -> Release:
    """Flip statuses to match the plan, then cut a release recording it.

    Refuses a plan with unrestorable entries unless `allow_partial` says the
    caller has seen them and accepts a partial restore. Rolling back to
    "nearly Monday" without saying so is how an incident gets a second cause.

    The new release is CUT, not rewound to: history is append-only, so
    returning to an earlier state is itself a deployment and is recorded as
    one. A log that could be rewound would lose the fact that the rollback
    happened, which is the single thing everyone asks about afterwards.
    """
    if plan.unrestorable and not allow_partial:
        listed = ", ".join(f"{slug} ({rev[:12]})" for slug, rev in plan.unrestorable[:5])
        raise ReleaseError(
            f"refusing to roll back to {plan.target.release_id[:12]}: "
            f"{len(plan.unrestorable)} lesson(s) were active at a revision this store "
            f"no longer holds ({listed}). Their text has been rewritten since, so "
            "flipping a status would put back a DIFFERENT rule under the same name. "
            "Restore the text first, or pass allow_partial to accept a partial "
            "restore knowingly."
        )

    for slug in plan.deactivate:
        _set_status(root, slug, "review", actor=actor,
                    reason=f"rolled back to {plan.target.release_id[:12]}")
    for slug in plan.reactivate:
        _set_status(root, slug, "active", actor=actor,
                    reason=f"rolled back to {plan.target.release_id[:12]}")

    return cut(
        root, base_id=current_id(root), actor=actor,
        reason=f"rollback to {plan.target.release_id[:12]}"
               + (" (partial)" if plan.unrestorable else ""),
    )


def _set_status(root: str, slug: str, status: str, *, actor: str, reason: str) -> None:
    path = lesson_io.lesson_path(root, slug)
    if path is None:
        raise ReleaseError(f"no lesson found for slug {slug!r}")
    with frontmatter.locked(path):
        fm, body = frontmatter.read(path)
        if (fm.get("status") or "active") == status:
            return
        fm["status"] = status
        lesson_io.write_lesson(path, fm, body, root=root, actor=actor, reason=reason)
