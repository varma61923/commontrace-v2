"""Named environments (dev/stage/prod) a release is promoted to, with
scheduled activation and an approval gate on protected ones -- audit
§4.2: "No dev/stage/prod environments, canary/ring targeting, scheduled
activation, approval chain UI."

PURE RECORD-KEEPING, LIKE RELEASES THEMSELVES BEFORE THIS MODULE EXISTED
-------------------------------------------------------------------------
This does not gate retrieval. `retrieve()`/`commontrace query` read a
lesson's own `status` today, exactly as they always have -- this module
records which release is promoted to which environment, and when,
rather than deciding what gets served. See `commontrace/release.py`'s
own module docstring ("WHAT THIS DOES NOT DO") for why that separation
is deliberate rather than an oversight one level up.

WHY RETRIEVAL STILL DOES NOT RESOLVE THROUGH THIS -- A SCOPE DECISION,
NOT (ANY LONGER) A HARD LIMIT
-------------------------------------------------------------------------
A `Release` (commontrace/release.py) pins `(slug, revision)` pairs --
`revision` is a content-addressed FINGERPRINT (commontrace/revision.py
hashes frontmatter+body), not the text itself. `plan_rollback` lives
with that honestly: it verifies a lesson's current text still matches a
release's pinned hash and REFUSES to restore one that has since changed,
rather than fabricating text it does not have -- exact-hash identity, not
"whatever was closest to that date," is the guarantee a rollback needs.

`commontrace/lesson_io.py`'s journal used to record only the fingerprint
transition (`from`/`to` hashes) and has since gained the actual
before/after TEXT on every entry going forward
(`content_as_of`/`commontrace lesson history --as-of`) -- so "what did
this lesson say on date X" is now answerable in general, for any lesson
whose relevant history was written after that field existed. What it
still does not give this module for free is a Release's exact-hash
guarantee: two edits on the same day, or a store whose history predates
the full-content field, make "the version active at this timestamp" a
weaker claim than "the exact bytes this hash names." So this module still
does not attempt point-in-time serving, canary traffic splitting, or ring
targeting through environments/releases -- not because the text is
unrecoverable in principle any more, but because a release's promotion
record is a hash-pinned guarantee this module is not willing to weaken
into a date-nearest-match one. This module answers a narrower, still-real
question: which release does this environment consider current, when did
that take effect, and what
is scheduled next -- an audit trail an operator can act on by hand,
today.

SCHEDULED ACTIVATION NEEDS NO SEPARATE FLIP STEP
-------------------------------------------------------------------------
A promotion just carries the moment it should take effect. `current()`
compares that moment against `now` on every call, so a promotion
scheduled for tomorrow has no effect until tomorrow arrives -- there is
no cron job, no daemon, and no "activate the due ones" sweep to run,
mirroring the same read-time-comparison trick this session's Hub-side
alerting (`hub/alerts.py`) and legal holds (`hub/retention.py`) already
use for the identical reason.
"""

from __future__ import annotations

import datetime
import json
import os

from commontrace import approval, paths, release

ENVIRONMENTS = ("dev", "stage", "prod")

#: Environments a promotion into is checked against the store's approval
#: policy, the same way activating one lesson already is -- prod is the
#: one whose blast radius audit 4.2 is actually concerned with.
PROTECTED_ENVIRONMENTS = ("prod",)

ENVIRONMENTS_FILENAME = "environments.jsonl"


class EnvironmentError(ValueError):
    """A promotion or lookup this module refuses on its own terms (an
    unknown environment, a release id that does not exist)."""


def environments_log_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), ENVIRONMENTS_FILENAME)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def read_all(root: str) -> list[dict]:
    """Every promotion ever recorded, in file order (not sorted by
    activation time -- see `current`/`pending` for that). A corrupt line
    is skipped rather than fatal, matching release.py/lesson_io.py's own
    convention: a damaged history is still worth more than none."""
    path = environments_log_path(root)
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("environment") and record.get("release_id"):
                out.append(record)
    return out


def promote(
    root: str, release_id: str, environment: str, *,
    actor: str = "", reason: str = "",
    activate_at: datetime.datetime | None = None,
) -> dict:
    """Record that `release_id` is (or will be, at `activate_at`) the
    release `environment` is running.

    Requires approval for a `PROTECTED_ENVIRONMENTS` target when the
    store's `approval-policy.yaml` says so (`commontrace/approval.py`):
    checked per lesson NEWLY entering the environment (present in
    `release_id`'s entries, absent from whatever the environment
    currently runs) against that lesson's own recorded authors -- reusing
    `approval.check`'s exact separation-of-duties and require-human
    rules, the same ones a single lesson's own activation already goes
    through, rather than a second, differently-behaved gate. A lesson
    already running in the environment is not re-litigated on every
    promotion; only what is actually new needs a second reviewer.
    """
    if environment not in ENVIRONMENTS:
        raise EnvironmentError(
            f"unknown environment {environment!r}; known environments: "
            + ", ".join(ENVIRONMENTS)
        )
    target = release.find(root, release_id)
    if target is None:
        raise EnvironmentError(f"no such release: {release_id}")

    if environment in PROTECTED_ENVIRONMENTS:
        policy = approval.load_policy(root)
        previous_id = current(root, environment)
        previous = release.find(root, previous_id) if previous_id else None
        previous_slugs = {e.slug for e in previous.entries} if previous else set()
        new_slugs = sorted({e.slug for e in target.entries} - previous_slugs)
        for slug in new_slugs:
            approval.check(
                policy, slug=slug, approver=actor,
                authors=approval.authors_of(root, slug),
            )

    moment = activate_at or _now()
    record = {
        "release_id": release_id,
        "environment": environment,
        "promoted_at": _now().isoformat(),
        "activate_at": moment.isoformat(),
        "actor": actor or "unknown",
        "reason": reason,
    }
    path = environments_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def current(root: str, environment: str, *, now: datetime.datetime | None = None) -> str | None:
    """The release id `environment` is currently running, or None if
    nothing has ever been promoted to it.

    Among every promotion whose `activate_at` has already passed, this
    is the one with the LATEST `activate_at` -- so a promotion scheduled
    for tomorrow has no effect today, and a promotion scheduled for
    yesterday (or with no `activate_at` at all, which defaults to the
    moment it was recorded) takes effect immediately, with no separate
    flip step for either case.
    """
    moment = now or _now()
    eligible = [
        r for r in read_all(root)
        if r["environment"] == environment
        and datetime.datetime.fromisoformat(r["activate_at"]) <= moment
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda r: r["activate_at"])["release_id"]


def pending(root: str, environment: str, *, now: datetime.datetime | None = None) -> list[dict]:
    """Promotions scheduled for the future, soonest first."""
    moment = now or _now()
    return sorted(
        (
            r for r in read_all(root)
            if r["environment"] == environment
            and datetime.datetime.fromisoformat(r["activate_at"]) > moment
        ),
        key=lambda r: r["activate_at"],
    )


def history(root: str, environment: str) -> list[dict]:
    """Every promotion ever recorded for this environment, oldest first
    (the order `read_all` already returns them in)."""
    return [r for r in read_all(root) if r["environment"] == environment]
