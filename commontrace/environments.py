from __future__ import annotations

import datetime
import json
import os

from commontrace import approval, paths, release

ENVIRONMENTS = ("dev", "stage", "prod")

PROTECTED_ENVIRONMENTS = ("prod",)

ENVIRONMENTS_FILENAME = "environments.jsonl"


class EnvironmentError(ValueError):
    ...


def environments_log_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), ENVIRONMENTS_FILENAME)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def read_all(root: str) -> list[dict]:
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
    return [r for r in read_all(root) if r["environment"] == environment]
