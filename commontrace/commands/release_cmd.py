"""`commontrace release` -- what the fleet is running, as one named thing.

A lesson has a slug (mutable), and its text has a revision
(commontrace/revision.py). Neither answers the question every operational
conversation is actually about: what was the fleet running on Monday, and
how do I put that back?

    release cut       record the active set as an immutable snapshot
    release list      every release, newest last
    release show      one release's contents
    release diff      what changed between two releases
    release rollback  return to an earlier release, and record having done so
    release promote   record which release a named environment is running
    release current   the release one environment is running right now
    release pending   promotions scheduled for the future, for one environment

See commontrace/release.py for why a release stores revisions rather than
text, and why rolling back appends rather than rewinds. See
commontrace/environments.py for why `promote` is pure record-keeping (it
does not change what retrieval serves) and for the real, storage-level
reason it cannot yet.
"""
from __future__ import annotations

import argparse
import datetime
import sys

from commontrace import approval, environments, paths, release


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "release",
        help="Snapshot, compare and roll back what the fleet is running.",
    )
    sub = p.add_subparsers(dest="release_cmd", required=True)

    cut = sub.add_parser(
        "cut", help="Record the currently active lessons as an immutable release."
    )
    cut.add_argument("--reason", default="", help="Why this set is being deployed.")
    cut.add_argument(
        "--base",
        default=None,
        help="The release you believe you are building on. Defaults to the current "
             "one. Given explicitly, a release cut from a store that has moved since "
             "is refused rather than overwriting someone else's deployment decision.",
    )
    cut.add_argument("--dest", default=None)
    cut.set_defaults(func=run_cut)

    listing = sub.add_parser("list", help="Every release, oldest first.")
    listing.add_argument("--dest", default=None)
    listing.set_defaults(func=run_list)

    show = sub.add_parser("show", help="One release's pinned set.")
    show.add_argument("release_id", nargs="?", default="", help="Default: the current one.")
    show.add_argument("--dest", default=None)
    show.set_defaults(func=run_show)

    diff = sub.add_parser("diff", help="What changed between two releases.")
    diff.add_argument("before")
    diff.add_argument(
        "after", nargs="?", default="",
        help="Default: the live active set, so `diff <id>` answers 'what has changed "
             "since that release'.",
    )
    diff.add_argument("--dest", default=None)
    diff.set_defaults(func=run_diff)

    rollback = sub.add_parser(
        "rollback", help="Return to an earlier release, and record having done so."
    )
    rollback.add_argument("release_id")
    rollback.add_argument(
        "--apply", action="store_true",
        help="Actually do it. Without this, prints the plan and changes nothing.",
    )
    rollback.add_argument(
        "--allow-partial", action="store_true",
        help="Proceed even when some lessons cannot be restored because their text "
             "has been rewritten since. Refused by default: flipping a status would "
             "put back a different rule under the same name.",
    )
    rollback.add_argument("--dest", default=None)
    rollback.set_defaults(func=run_rollback)

    promote = sub.add_parser(
        "promote",
        help="Record which release a named environment (dev/stage/prod) is running.",
    )
    promote.add_argument("release_id")
    promote.add_argument("environment", choices=environments.ENVIRONMENTS)
    promote.add_argument("--reason", default="")
    promote.add_argument(
        "--at", default=None,
        help="ISO-8601 timestamp to schedule this for, instead of immediately. "
             "Needs no separate activation step: `release current` simply starts "
             "returning this release once that moment arrives.",
    )
    promote.add_argument("--dest", default=None)
    promote.set_defaults(func=run_promote)

    env_current = sub.add_parser(
        "current", help="The release one environment is running right now.",
    )
    env_current.add_argument("environment", choices=environments.ENVIRONMENTS)
    env_current.add_argument("--dest", default=None)
    env_current.set_defaults(func=run_env_current)

    env_pending = sub.add_parser(
        "pending", help="Promotions scheduled for the future, for one environment.",
    )
    env_pending.add_argument("environment", choices=environments.ENVIRONMENTS)
    env_pending.add_argument("--dest", default=None)
    env_pending.set_defaults(func=run_env_pending)


def _actor() -> str:
    import getpass

    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        return "cli"


def _short(release_id: str) -> str:
    return release_id[:12]


def run_cut(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    base = args.base if args.base is not None else release.current_id(root)
    try:
        cut = release.cut(root, base_id=base, actor=_actor(), reason=args.reason)
    except release.StaleBaseError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    except release.ReleaseError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    previous = None
    history = release.read_all(root)
    if len(history) > 1:
        previous = history[-2]
    changes = release.diff(previous, cut)
    print(f"[commontrace] release {_short(cut.release_id)} "
          f"({len(cut.entries)} active lesson(s), {changes.summary()})")
    for entry in changes.added:
        print(f"  + {entry.slug}")
    for entry in changes.removed:
        print(f"  - {entry.slug}")
    for slug, _old, _new in changes.changed:
        print(f"  ~ {slug} (rewritten)")
    return 0


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    history = release.read_all(root)
    if not history:
        print("[commontrace] no releases yet. `commontrace release cut` records the "
              "currently active lessons as one.")
        return 0
    previous = None
    for item in history:
        changes = release.diff(previous, item)
        marker = "*" if item is history[-1] else " "
        print(f"{marker} {_short(item.release_id)}  {item.created_at}  "
              f"{len(item.entries):3d} lesson(s)  {changes.summary()}"
              + (f"  -- {item.reason}" if item.reason else ""))
        previous = item
    return 0


def run_show(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        item = (
            release.find(root, args.release_id) if args.release_id
            else release.current(root)
        )
    except release.ReleaseError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if item is None:
        print("[commontrace] no such release.", file=sys.stderr)
        return 1

    print(f"release {item.release_id}")
    print(f"  parent:  {_short(item.parent_id)}")
    print(f"  cut:     {item.created_at} by {item.actor}")
    if item.reason:
        print(f"  reason:  {item.reason}")
    print(f"  lessons: {len(item.entries)}")
    for entry in item.entries:
        print(f"    {entry.slug}  @{entry.revision[:12]}")
    return 0


def run_diff(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        before = release.find(root, args.before)
        if before is None:
            print(f"[commontrace] no such release: {args.before}", file=sys.stderr)
            return 1
        if args.after:
            after = release.find(root, args.after)
            if after is None:
                print(f"[commontrace] no such release: {args.after}", file=sys.stderr)
                return 1
        else:
            # The live set, as an unrecorded release: "what has changed since
            # that release" is the question asked far more often than
            # "what changed between two things I already wrote down".
            after = release.Release(
                release_id="(live)", parent_id=before.release_id,
                entries=release.active_entries(root), created_at="", actor="", reason="",
            )
    except release.ReleaseError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    changes = release.diff(before, after)
    label = "(live active set)" if after.release_id == "(live)" else _short(after.release_id)
    print(f"[commontrace] {_short(before.release_id)} -> {label}: {changes.summary()}")
    for entry in changes.added:
        print(f"  + {entry.slug}  @{entry.revision[:12]}")
    for entry in changes.removed:
        print(f"  - {entry.slug}  @{entry.revision[:12]}")
    for slug, old, new in changes.changed:
        print(f"  ~ {slug}  @{old[:12]} -> @{new[:12]}")
    return 0


def run_rollback(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        target = release.find(root, args.release_id)
    except release.ReleaseError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if target is None:
        print(f"[commontrace] no such release: {args.release_id}", file=sys.stderr)
        return 1

    plan = release.plan_rollback(root, target)
    print(f"[commontrace] rollback to {_short(target.release_id)} "
          f"({target.created_at}) would:")
    if plan.empty:
        print("  change nothing -- the active set already matches it.")
    for slug in plan.deactivate:
        print(f"  - deactivate {slug}")
    for slug in plan.reactivate:
        print(f"  + reactivate {slug}")
    for slug, revision in plan.unrestorable:
        print(f"  ! {slug} cannot be restored: it was active at @{revision[:12]}, "
              "and its text has been rewritten since")

    if not args.apply:
        print("\n  Nothing changed. Re-run with --apply to do it.")
        return 0

    try:
        cut = release.apply_rollback(
            root, plan, actor=_actor(), allow_partial=args.allow_partial
        )
    except release.ReleaseError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    print(f"\n[commontrace] rolled back; recorded as release {_short(cut.release_id)}")
    print("  History is append-only: returning to an earlier state is itself a "
          "deployment, and is recorded as one.")
    return 0


def run_promote(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    activate_at = None
    if args.at:
        try:
            activate_at = datetime.datetime.fromisoformat(args.at)
        except ValueError:
            print(f"[commontrace] --at must be an ISO-8601 timestamp, got {args.at!r}",
                  file=sys.stderr)
            return 1
    try:
        record = environments.promote(
            root, args.release_id, args.environment,
            actor=_actor(), reason=args.reason, activate_at=activate_at,
        )
    except (environments.EnvironmentError, approval.ApprovalDenied) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    when = "immediately" if not args.at else f"at {record['activate_at']}"
    print(f"[commontrace] {args.environment} -> {_short(record['release_id'])}, {when}.")
    return 0


def run_env_current(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    release_id = environments.current(root, args.environment)
    if release_id is None:
        print(f"[commontrace] nothing has ever been promoted to {args.environment!r}.")
        return 0
    print(f"[commontrace] {args.environment}: {_short(release_id)}")
    return 0


def run_env_pending(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    upcoming = environments.pending(root, args.environment)
    if not upcoming:
        print(f"[commontrace] nothing scheduled for {args.environment!r}.")
        return 0
    for record in upcoming:
        print(f"  {record['activate_at']}  -> {_short(record['release_id'])}"
              + (f"  -- {record['reason']}" if record["reason"] else ""))
    return 0
