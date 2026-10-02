"""The one place a lesson is written, so what it said is never lost."""

from __future__ import annotations

import datetime
import json
import os
import re

from commontrace import frontmatter, paths, revision

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def canonical_slug(raw: str) -> str:
    """One identity for `foo`, `lesson_foo` and `lesson_foo.md`."""
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
    """Write one lesson, journaling the change if the content moved."""
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
        if before_fm is not None and before_body is not None:
            record["before_frontmatter"] = before_fm
            record["before_body"] = before_body
        _journal(root, record)
    frontmatter.write(path, fm, body)
    return after


def _journal(root: str, record: dict) -> None:
    path = revisions_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
    """One lesson's content changes, oldest first."""
    records, _ = read_revisions(root)
    want = canonical_slug(slug)
    return [r for r in records if canonical_slug(str(r.get("lesson") or "")) == want]


def _parse_at(value: str) -> datetime.datetime | None:
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
    ...


def content_as_of(root: str, slug: str, at: str) -> tuple[dict, str]:
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

    path = lesson_path(root, slug)
    if path is None:
        raise ContentAsOfError(f"no lesson found for slug '{slug}'.")
    try:
        return frontmatter.read(path)
    except Exception as exc:  # noqa: BLE001 - surfaced as this function's own error
        raise ContentAsOfError(f"could not read '{slug}': {exc}") from exc
