"""What the fleet was running, as one named, immutable thing."""

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
    """The store moved between reading it and cutting a release from it."""

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
    """Content-addressed, over the parent and the pinned set."""
    rows = _FIELD_SEP.join(
        f"{e.slug}={e.revision}" for e in sorted(entries, key=lambda e: e.slug)
    )
    return hashlib.sha256(
        (parent_id + _FIELD_SEP + rows).encode("utf-8")
    ).hexdigest()


def active_entries(root: str) -> tuple[Entry, ...]:
    """Every active lesson in the store, pinned to its current revision."""
    entries: list[Entry] = []
    pattern = os.path.join(paths.lessons_dir(root), "*.md")
    for path in sorted(glob.glob(pattern)):
        try:
            fm, body = frontmatter.read(path)
        except (frontmatter.FrontmatterError, OSError, UnicodeDecodeError):
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
    """Record the active set as an immutable release."""
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
    """What `after` changed relative to `before`."""
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
    unrestorable: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return not self.unrestorable

    @property
    def empty(self) -> bool:
        return not (self.deactivate or self.reactivate or self.unrestorable)


def plan_rollback(root: str, target: Release) -> RollbackPlan:
    """Work out what returning to `target` involves, without doing it."""
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
    """Flip statuses to match the plan, then cut a release recording it."""
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
