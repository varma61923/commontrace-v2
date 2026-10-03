"""`commontrace memory`: the store as a versioned filesystem (history, validation,
conflict repair, signed handoffs)."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import memfs, memory_git, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "memory",
        help="Version the store with git: validate, commit, diff, restore, repair conflicts, hand off.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    def cmd(name: str, help_: str, func) -> argparse.ArgumentParser:
        q = sub.add_parser(name, help=help_)
        q.add_argument("--dest", default=None)
        q.add_argument("--json", action="store_true", help="Print JSON.")
        q.set_defaults(func=func)
        return q

    cmd("init", "Make the store its own git repository and install the validating pre-commit hook.", run_init)
    cmd("status", "Show the head commit and uncommitted memory changes.", run_status)
    q = cmd("log", "Show recent memory commits.", run_log)
    q.add_argument("-n", type=int, default=10)
    q = cmd("diff", "Show what changed in memory/ since a revision (default: uncommitted changes).", run_diff)
    q.add_argument("rev", nargs="?", default=None)
    q = cmd("commit", "Validate and commit every memory change.", run_commit)
    q.add_argument("-m", "--message", default="commontrace: memory update")
    q.add_argument("--no-verify", action="store_true", help="Skip validation and the pre-commit hook.")
    q = cmd("restore", "Put memory/ back to a revision as a new commit (history is kept).", run_restore)
    q.add_argument("rev")
    q = cmd("validate", "Check memory files against the limits in memory/memfs.json.", run_validate)
    q.add_argument("--staged", action="store_true", help="Only the files staged for commit (the hook uses this).")
    cmd("repair", "Resolve merge conflicts: union JSONL logs, park the other side of anything else.", run_repair)
    q = cmd("keygen", "Write a handoff signing key to memory/.handoff_key (mode 0600, never committed).", run_keygen)
    q.add_argument("--force", action="store_true")

    q = sub.add_parser("handoff", help="Hand a committed memory state to another agent, or check one.")
    hsub = q.add_subparsers(dest="action", required=True)
    h = hsub.add_parser("create", help="Sign a token naming the current memory commit.")
    h.add_argument("--to", required=True, dest="audience", help="Who may take the memory over.")
    h.add_argument("--from", default="", dest="issuer")
    h.add_argument("--ttl", type=int, default=3600, help="Seconds the token holds (60 to 30 days).")
    h.add_argument("--scope", action="append", default=[], help="Paths covered (default memory/; repeatable).")
    h.add_argument("--dest", default=None)
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=run_handoff_create)
    h = hsub.add_parser("verify", help="Verify a handoff token against this store.")
    h.add_argument("token")
    h.add_argument("--audience", default=None, help="Refuse a token addressed to anyone else.")
    h.add_argument("--dest", default=None)
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=run_handoff_verify)


def _guard(func):
    def run(args: argparse.Namespace) -> int:
        try:
            return func(args, paths.resolve_root(args.dest))
        except memfs.MemfsError as exc:
            print(f"[commontrace] memory: {exc}", file=sys.stderr)
            return 1
    run.__name__ = func.__name__
    return run


def _emit(args: argparse.Namespace, data, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if args.json else text)


@_guard
def run_init(args, root) -> int:
    out = memfs.init(root)
    _emit(args, out, f"memory is versioned in {root}\nhook: {out['hook']}"
                     + ("\ncommitted the current state" if out["committed"] else ""))
    return 0


@_guard
def run_status(args, root) -> int:
    out = memfs.status(root)
    lines = [f"head: {out['head'] or '(no commits)'}"]
    lines += [f"  changed: {c}" for c in out["changed"]] or ["  clean"]
    _emit(args, out, "\n".join(lines))
    return 0


@_guard
def run_log(args, root) -> int:
    memfs.require_repo(root)
    out = memory_git.log(root, args.n)
    if not out.get("ok"):
        raise memfs.MemfsError(out.get("error", "git log failed"))
    lines = [f"{e.get('hash', '')[:12]}  {e.get('date', '')}  {e.get('subject', '')}" for e in out["entries"]]
    _emit(args, out, "\n".join(lines) or "(no commits)")
    return 0


@_guard
def run_diff(args, root) -> int:
    text = memfs.diff(root, args.rev)
    _emit(args, {"diff": text}, text.rstrip() or "(no changes)")
    return 0


@_guard
def run_commit(args, root) -> int:
    out = memfs.commit(root, args.message, verify=not args.no_verify)
    _emit(args, out, f"committed {out['commit'][:12]}" if out.get("committed") else "nothing to commit")
    return 0


@_guard
def run_restore(args, root) -> int:
    out = memfs.restore(root, args.rev)
    if not out.get("ok"):
        raise memfs.MemfsError(out.get("error", "restore failed"))
    _emit(args, out, f"restored memory as {out['commit'][:12]}" if out.get("committed")
          else "memory already matches that revision")
    return 0


@_guard
def run_validate(args, root) -> int:
    if args.staged:
        memfs.require_repo(root)
        files = [f for f in memfs._staged(root) if f.startswith(paths.memory_dir(root))]
        report = memfs.validate(root, files)
    else:
        report = memfs.validate(root)
    data = {"ok": report.ok, "checked": report.checked, "problems": report.problems}
    if args.json:
        print(json.dumps(data, indent=2))
    elif report.ok:
        print(f"memory ok ({report.checked} file(s) checked)")
    else:
        print("memory validation failed:", file=sys.stderr)
        for problem in report.problems:
            print(f"  {problem}", file=sys.stderr)
    return 0 if report.ok else 1


@_guard
def run_repair(args, root) -> int:
    out = memfs.repair(root)
    lines = [f"merged: {p}" for p in out["repaired"]] + [f"kept ours, theirs parked at: {p}" for p in out["parked"]]
    _emit(args, out, "\n".join(lines) or "no conflicts")
    return 0


@_guard
def run_keygen(args, root) -> int:
    path = memfs.keygen(root, force=args.force)
    _emit(args, {"key_file": path}, f"wrote {path} (keep it out of version control; share it only with "
                                    "agents that should accept your handoffs)")
    return 0


@_guard
def run_handoff_create(args, root) -> int:
    token = memfs.handoff(root, audience=args.audience, issuer=args.issuer, ttl_seconds=args.ttl,
                          scope=args.scope or None)
    _emit(args, {"token": token}, token)
    return 0


@_guard
def run_handoff_verify(args, root) -> int:
    out = memfs.verify_handoff(root, args.token, audience=args.audience)
    text = (f"valid handoff from {out['iss']} to {out['aud']} of {out['commit'][:12]}"
            + ("; memory has changed since" if out["drift"] else "; memory is unchanged since"))
    _emit(args, out, text)
    return 0
