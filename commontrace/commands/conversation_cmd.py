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
    r.add_argument("--now", default=None,
                   help="historical cutoff and reference time (default: the latest message, without a cutoff)")
    r.add_argument("--rerank", choices=("auto", "none", "cross-encoder", "cross-encoder-fast"), default="auto",
                   help="second-stage reranker (auto: the accurate one when the attention extra is installed)")
    r.add_argument("--lexical", action="store_true", help="keyword search only, no embedding model")
    _filters(r)
    r.add_argument("--json", action="store_true")
    r.add_argument("--dest", default=None)
    r.set_defaults(func=run_recall)

    q = sub.add_parser("answer", help="Answer a question from memory with the configured model (COMMONTRACE_LLM_*).")
    q.add_argument("space", help=SPACE_HELP)
    q.add_argument("question")
    q.add_argument("--rounds", type=int, default=1,
                   help="up to 4: the model may ask for follow-up searches before answering")
    q.add_argument("--budget", type=int, default=None)
    q.add_argument("--now", default=None)
    _filters(q)
    q.add_argument("--json", action="store_true")
    q.add_argument("--dest", default=None)
    q.set_defaults(func=run_answer)

    m = sub.add_parser("summarize", help="Summarise sessions; recall shows a session's summary under its header.")
    m.add_argument("space", help=SPACE_HELP)
    m.add_argument("--session", action="append", default=[], help="only this session (repeatable)")
    m.add_argument("--model", action="store_true", help="have the configured model write them (default: extractive)")
    m.add_argument("--force", action="store_true", help="rewrite summaries that are already current")
    m.add_argument("--dest", default=None)
    m.set_defaults(func=run_summarize)

    x = sub.add_parser("extract", help="Distil dated memories from new messages with the configured model.")
    x.add_argument("space", help=SPACE_HELP)
    x.add_argument("--session", action="append", default=[], help="only this session (repeatable)")
    x.add_argument("--dest", default=None)
    x.set_defaults(func=run_extract)

    e = sub.add_parser("export", help="Write a space as JSONL (one session per line).")
    e.add_argument("space", help=SPACE_HELP)
    e.add_argument("--out", default="-", help="file to write, - for stdout (default)")
    e.add_argument("--dest", default=None)
    e.set_defaults(func=run_export)

    i = sub.add_parser("import", help="Read sessions written by `conversation export` into a space.")
    i.add_argument("space", help=SPACE_HELP)
    i.add_argument("file", help="JSONL from `conversation export`, - for stdin")
    i.add_argument("--dest", default=None)
    i.set_defaults(func=run_import)

    pr = sub.add_parser("promote", help="Copy the current profile into this store's atomic facts.")
    pr.add_argument("space", help=SPACE_HELP)
    pr.add_argument("--scope", default="", help="fact scope (default conversation:<space>)")
    pr.add_argument("--dest", default=None)
    pr.set_defaults(func=run_promote)

    g = sub.add_parser("forget", help="Delete messages past their expiry, or said before a date.")
    g.add_argument("space", help=SPACE_HELP)
    g.add_argument("--expired", action="store_true", help="messages whose `expires` has passed")
    g.add_argument("--before", default=None, help="messages said before this date")
    g.add_argument("--dest", default=None)
    g.set_defaults(func=run_forget)

    s = sub.add_parser("sessions", help="List a space's sessions.")
    s.add_argument("space", help=SPACE_HELP)
    s.add_argument("--json", action="store_true")
    s.add_argument("--dest", default=None)
    s.set_defaults(func=run_sessions)

    f = sub.add_parser("profile", help="What the user has said about themselves, oldest first.")
    f.add_argument("space", help=SPACE_HELP)
    f.add_argument("--history", action="store_true", help="include statements a newer one replaced")
    f.add_argument("--as-of", default=None, help="show the profile as it stood at this date")
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


def _filters(p) -> None:
    p.add_argument("--session", action="append", default=[], help="only these sessions (repeatable)")
    p.add_argument("--speaker", action="append", default=[], help="only these speakers (repeatable)")
    p.add_argument("--since", default=None, help="only messages said at or after this date")
    p.add_argument("--until", default=None, help="only messages said at or before this date")


def _options(args):
    from commontrace.conversation import Options

    opts = Options(rerank=None if getattr(args, "rerank", "auto") == "none" else getattr(args, "rerank", "auto"),
                   embedder=None if getattr(args, "lexical", False) else "auto",
                   sessions=tuple(args.session), speakers=tuple(args.speaker), since=args.since, until=args.until)
    if args.budget is not None:
        if args.budget < 50:
            raise ValueError("--budget must be at least 50 tokens")
        opts.budget = args.budget
    return opts


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
    from commontrace.conversation import ConversationError, recall

    try:
        opts = _options(args)
        with _store(args, create=False) as store:
            result = recall(store, args.question, now=args.now, options=opts)
    except (ConversationError, ValueError) as exc:
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


def run_answer(args) -> int:
    from commontrace import llm
    from commontrace.conversation import ConversationError
    from commontrace.conversation.answer import answer

    try:
        opts = _options(args)
        with _store(args, create=False) as store:
            out = answer(store, args.question, now=args.now, options=opts, rounds=args.rounds)
    except (ConversationError, ValueError, llm.LLMUnavailable) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(out["answer"])
        extra = f", follow-ups: {'; '.join(out['follow_ups'])}" if out["follow_ups"] else ""
        print(f"[commontrace] from {len(out['turns'])} turn(s), ~{out['tokens']} tokens of memory{extra}.",
              file=sys.stderr)
    return 0


def run_summarize(args) -> int:
    from commontrace import llm
    from commontrace.conversation import ConversationError
    from commontrace.conversation.summary import summarize

    try:
        with _store(args, create=False) as store:
            out = summarize(store, args.session or None, method="model" if args.model else "extractive",
                            force=args.force)
    except (ConversationError, llm.LLMUnavailable) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    print(f"[commontrace] {out['summarized']} session(s) summarised ({out['method']}), "
          f"{out['unchanged']} already current.")
    return 0


def run_extract(args) -> int:
    from commontrace import llm
    from commontrace.conversation import ConversationError
    from commontrace.conversation.extract import extract

    try:
        with _store(args, create=False) as store:
            out = extract(store, args.session or None)
    except (ConversationError, llm.LLMUnavailable) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    note = f", {out['refused']} refused by the injection screen" if out["refused"] else ""
    print(f"[commontrace] {out['memories']} memory(ies) from {out['calls']} model call(s){note}.")
    return 0


def run_export(args) -> int:
    from commontrace.conversation import ConversationError

    try:
        with _store(args, create=False) as store:
            lines = [json.dumps(row, ensure_ascii=False) for row in store.export()]
    except ConversationError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    text = "\n".join(lines) + ("\n" if lines else "")
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[commontrace] {len(lines)} session(s) written to {args.out}.", file=sys.stderr)
    return 0


def run_import(args) -> int:
    from commontrace.conversation import ConversationError

    try:
        raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        added = sessions = 0
        with _store(args) as store:
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("messages"), list):
                    raise ValueError("each line must be a session object with a messages list")
                added += store.add(str(row.get("session") or ""), row["messages"],
                                   session_at=row.get("started_at"))["added"]
                if row.get("summary"):
                    store.set_summary(str(row["session"]), str(row["summary"]), "imported")
                sessions += 1
    except (ConversationError, ValueError, OSError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    print(f"[commontrace] {added} message(s) in {sessions} session(s) imported into {args.space}.")
    return 0


def run_promote(args) -> int:
    from commontrace.conversation import ConversationError

    try:
        with _store(args, create=False) as store:
            out = store.promote(scope=args.scope)
    except (ConversationError, ValueError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    print(f"[commontrace] {out['promoted']} fact(s) added, {out['reinforced']} reinforced.")
    return 0


def run_forget(args) -> int:
    import datetime as dt

    from commontrace.conversation import ConversationError

    if not args.expired and not args.before:
        print("[commontrace] give --expired, --before DATE, or both", file=sys.stderr)
        return 2
    try:
        with _store(args, create=False) as store:
            n = store.purge(before=args.before,
                            expired_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) if args.expired else None)
    except ConversationError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    print(f"[commontrace] {n} message(s) deleted from {args.space}.")
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
            facts = store.facts(history=args.history, as_of=args.as_of)
    except ConversationError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(facts, indent=2))
        return 0
    for f in facts:
        replaced = "\t(replaced)" if f["superseded_by"] else ""
        print(f"{(f['at'] or '')[:10]}\t{f['kind']}\t{f['statement']}{replaced}")
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
