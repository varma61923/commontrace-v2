"""`commontrace conversation`: remember what was said, recall it into a small dated context."""
from __future__ import annotations

import argparse
import json
import os
import sys

from commontrace import paths

SPACE_HELP = "the conversation space: one user, agent or thread (letters, digits, . _ -)"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "conversation",
        help="Conversation memory: store messages with their dates, recall what answers a question.")
    sub = p.add_subparsers(dest="subcommand", required=True)

    a = sub.add_parser("add", help="Add messages to a session (JSON list or JSONL on a file or stdin).")
    a.add_argument("space", help=SPACE_HELP)
    a.add_argument("--session", required=True, help="session id; messages append to it in order")
    a.add_argument("--at", default=None, help="when the session took place (ISO, 2023/05/20 10:00, '8 May 2023')")
    a.add_argument("--file", default="-", help="messages as JSON or JSONL, - for stdin (default)")
    a.add_argument("--text", default=None, help="add one message with this text instead of reading --file")
    a.add_argument("--speaker", default="user", help="speaker of --text (default user)")
    a.add_argument("--dest", default=None)
    a.set_defaults(func=run_add)

    r = sub.add_parser("recall", help="The turns that answer a question, as a dated, budgeted context.")
    r.add_argument("space", help=SPACE_HELP)
    r.add_argument("question")
    r.add_argument("--budget", type=int, default=None, help="context size in tokens (default 1500)")
    r.add_argument("--now", default=None, help="when the question is asked (default: the latest message)")
    r.add_argument("--rerank", choices=("auto", "none", "cross-encoder", "cross-encoder-fast"), default="auto",
                   help="second-stage reranker (auto: the accurate one when the attention extra is installed)")
    r.add_argument("--lexical", action="store_true", help="keyword search only, no embedding model")
    r.add_argument("--json", action="store_true")
    r.add_argument("--dest", default=None)
    r.set_defaults(func=run_recall)

    s = sub.add_parser("sessions", help="List a space's sessions.")
    s.add_argument("space", help=SPACE_HELP)
    s.add_argument("--json", action="store_true")
    s.add_argument("--dest", default=None)
    s.set_defaults(func=run_sessions)

    f = sub.add_parser("profile", help="What the user has said about themselves, oldest first.")
    f.add_argument("space", help=SPACE_HELP)
    f.add_argument("--json", action="store_true")
    f.add_argument("--dest", default=None)
    f.set_defaults(func=run_profile)

    d = sub.add_parser("delete", help="Delete a session, or a whole space with --all.")
    d.add_argument("space", help=SPACE_HELP)
    d.add_argument("--session", default=None)
    d.add_argument("--all", action="store_true", help="delete every session in the space")
    d.add_argument("--dest", default=None)
    d.set_defaults(func=run_delete)

    ls = sub.add_parser("spaces", help="List spaces with stored conversations.")
    ls.add_argument("--dest", default=None)
    ls.set_defaults(func=run_spaces)


def _store(args, create: bool = True):
    from commontrace.conversation import Store

    return Store(paths.resolve_root(args.dest), args.space, create=create)


def _read_messages(path: str) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        data = json.loads(raw)
    else:
        data = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not all(isinstance(m, dict) for m in data):
        raise ValueError("each message must be a JSON object with text (or content)")
    return data


def run_add(args) -> int:
    from commontrace.conversation import ConversationError

    try:
        messages = [{"speaker": args.speaker, "text": args.text}] if args.text is not None \
            else _read_messages(args.file)
        with _store(args) as store:
            result = store.add(args.session, messages, session_at=args.at)
    except (ConversationError, ValueError, OSError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    note = f", {result['secrets_redacted']} secret(s) redacted" if result["secrets_redacted"] else ""
    print(f"[commontrace] {result['added']} message(s) added to {args.space}/{args.session}"
          f" ({result['skipped']} already stored or empty{note}).")
    return 0


def run_recall(args) -> int:
    from commontrace.conversation import ConversationError, Options, recall

    opts = Options(rerank=None if args.rerank == "none" else args.rerank,
                   embedder=None if args.lexical else "auto")
    if args.budget is not None:
        if args.budget < 50:
            print("[commontrace] --budget must be at least 50 tokens", file=sys.stderr)
            return 2
        opts.budget = args.budget
    try:
        with _store(args, create=False) as store:
            result = recall(store, args.question, now=args.now, options=opts)
    except ConversationError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
    else:
        print(result.context or "(nothing stored answers this)")
        withheld = result.explain.get("withheld")
        note = f"; {len(withheld)} turn(s) withheld by the injection screen" if withheld else ""
        print(f"\n[commontrace] {len(result.turns)} turn(s), ~{result.tokens} tokens{note}.", file=sys.stderr)
    return 0


def run_sessions(args) -> int:
    from commontrace.conversation import ConversationError

    try:
        with _store(args, create=False) as store:
            rows, stats = store.sessions(), store.stats()
    except ConversationError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"stats": stats, "sessions": rows}, indent=2))
        return 0
    for row in rows:
        print(f"{row['id']}\t{row['started_at'] or '-'}\t{row['turns']} turn(s)")
    print(f"[commontrace] {stats['sessions']} session(s), {stats['turns']} turn(s), "
          f"{stats['facts']} profile statement(s).", file=sys.stderr)
    return 0


def run_profile(args) -> int:
    from commontrace.conversation import ConversationError

    try:
        with _store(args, create=False) as store:
            facts = store.facts()
    except ConversationError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(facts, indent=2))
        return 0
    for f in facts:
        print(f"{(f['at'] or '')[:10]}\t{f['kind']}\t{f['statement']}")
    return 0


def run_delete(args) -> int:
    from commontrace.conversation import ConversationError

    if bool(args.session) == bool(args.all):
        print("[commontrace] give exactly one of --session or --all", file=sys.stderr)
        return 2
    try:
        if args.all:
            with _store(args, create=False) as store:
                path = store.path
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)
            print(f"[commontrace] deleted space {args.space}.")
            return 0
        with _store(args, create=False) as store:
            n = store.delete_session(args.session)
    except ConversationError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if not n:
        print(f"[commontrace] no session {args.session!r} in {args.space}", file=sys.stderr)
        return 1
    print(f"[commontrace] deleted {n} turn(s) from {args.space}/{args.session}.")
    return 0


def run_spaces(args) -> int:
    from commontrace.conversation import spaces

    for name in spaces(paths.resolve_root(args.dest)):
        print(name)
    return 0
