"""`commontrace dream`: the scheduled consolidation pass, and the recipe to schedule it."""
from __future__ import annotations

import argparse
import datetime
import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

from commontrace import failure_signals, frontmatter, paths

RECIPES = ("cron", "systemd", "github-actions")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "dream",
        help="One consolidation pass for a schedule: draft from failures, propose fusions, list what waits "
             "for review. Writes only status=review drafts.",
    )
    p.add_argument("--no-draft", action="store_true",
                   help="Do not call a model: report only (the default when COMMONTRACE_LLM_API_KEY, or a cloud "
                        "provider, is not configured).")
    p.add_argument("--recipe", choices=RECIPES, default=None,
                   help="Print the schedule entry for this runner and exit; installs nothing.")
    p.add_argument("--every", choices=("daily", "weekly"), default="weekly", help="With --recipe.")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def recipe(kind: str, every: str, dest: str | None) -> str:
    target = f" --dest {dest}" if dest else ""
    command = f"commontrace dream{target}"
    if kind == "cron":
        when = "17 3 * * *" if every == "daily" else "17 3 * * 1"
        return f"# commontrace dream: drafts only, nothing is activated\n{when} {command} >> dream.log 2>&1\n"
    if kind == "systemd":
        calendar = "daily" if every == "daily" else "Mon 03:17"
        return (
            "# ~/.config/systemd/user/commontrace-dream.service\n[Unit]\nDescription=CommonTrace dream pass\n\n"
            f"[Service]\nType=oneshot\nExecStart={command}\n\n"
            "# ~/.config/systemd/user/commontrace-dream.timer\n[Unit]\nDescription=Schedule the dream pass\n\n"
            f"[Timer]\nOnCalendar={calendar}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    cron = "17 3 * * *" if every == "daily" else "17 3 * * 1"
    return (
        "# .github/workflows/commontrace-dream.yml: runs on a schedule, opens no PR, activates nothing\n"
        "name: commontrace dream\non:\n  schedule:\n"
        f"    - cron: \"{cron}\"\n  workflow_dispatch:\n\npermissions:\n  contents: read\n\njobs:\n  dream:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: python3 -m pip install commontrace\n"
        f"      - run: {command}\n"
        "        env:\n          COMMONTRACE_LLM_API_KEY: ${{ secrets.COMMONTRACE_LLM_API_KEY }}\n"
        "      - uses: actions/upload-artifact@v4\n        with:\n          name: dream\n"
        "          path: memory/dream/\n")


def _llm_configured() -> bool:
    from commontrace import llm

    try:
        llm.load_config()
        return True
    except llm.LLMUnavailable:
        return False


def run(args: argparse.Namespace) -> int:
    if args.recipe:
        print(recipe(args.recipe, args.every, args.dest), end="")
        return 0
    from commontrace.cli import main as cli

    root = paths.resolve_root(args.dest)
    draft = not args.no_draft and _llm_configured()
    now = datetime.datetime.now(datetime.timezone.utc)
    lines = [f"# Dream pass {now.strftime('%Y-%m-%d %H:%M')}Z", ""]
    before = set(_review(root))

    signals, _ = failure_signals.build_signals(root)
    lines += [f"## Failure signals ({len(signals)})", ""]
    lines += [f"- {s.name} ({s.size} traces, {s.trend})" for s in signals] or ["- none"]
    lines.append("")
    if signals and draft:
        for s in signals:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli(["distill", "--draft", "--signal", s.name, "--dest", root])
    elif signals:
        lines += ["_No model configured, so nothing was drafted. Run `commontrace distill --failed` for "
                  "evidence-only candidates._", ""]

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        cli(["consolidate", *(["--draft"] if draft else []), "--dest", root])
    lines += ["## Consolidation", "", "```", out.getvalue().strip(), "```", ""]

    after = _review(root)
    new = sorted(set(after) - before)
    lines += [f"## Waiting for review ({len(after)}; {len(new)} new this pass)", ""]
    lines += [f"- {slug}" + (" (new)" if slug in new else "") for slug in sorted(after)] or ["- none"]
    lines += ["", "Review with `commontrace lesson list --status review`; nothing here is active."]
    report_dir = os.path.join(paths.memory_dir(root), "dream")
    os.makedirs(report_dir, exist_ok=True)
    report = os.path.join(report_dir, f"{now.strftime('%Y-%m-%d')}.md")
    with open(report, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"[commontrace] dream: {len(signals)} signal(s), {len(new)} new draft(s), {len(after)} awaiting review. "
          f"Report: {report}" + ("" if draft else " (no model: report only)"))
    return 0


def _review(root: str) -> list[str]:
    ldir = paths.lessons_dir(root)
    if not os.path.isdir(ldir):
        return []
    out = []
    for name in os.listdir(ldir):
        if name.startswith("lesson_") and name.endswith(".md") and name != "lesson_template.md":
            try:
                fm, _ = frontmatter.read(os.path.join(ldir, name))
            except Exception as exc:  # noqa: BLE001 - one unreadable lesson must not stop the pass
                print(f"[commontrace] dream: skipping unreadable {name}: {exc}", file=sys.stderr)
                continue
            if fm.get("status") == "review":
                out.append(name.removesuffix(".md"))
    return out
