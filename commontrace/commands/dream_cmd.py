"""`commontrace dream`: the scheduled consolidation pass, and the recipe to schedule it."""
from __future__ import annotations

import argparse
import datetime
import io
import os
import sys
import time
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
    p.add_argument("--max-model-calls", type=int, default=10)
    p.add_argument("--max-tokens", type=int, default=50000)
    p.add_argument("--max-cost-usd", type=float, default=None)
    p.add_argument("--max-seconds", type=float, default=120,
                   help="Stop starting work after this budget; an in-flight provider call may finish later.")
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
    from commontrace import _jsonl
    from commontrace.llm_runtime import Budget, LLMRuntime

    if args.recipe:
        print(recipe(args.recipe, args.every, args.dest), end="")
        return 0
    root = paths.resolve_root(args.dest)
    runtime = LLMRuntime(root, "dream-" + str(time.time_ns()), budget=Budget(
        calls=getattr(args, "max_model_calls", 10), tokens=getattr(args, "max_tokens", 50000),
        cost_usd=getattr(args, "max_cost_usd", None), seconds=getattr(args, "max_seconds", 120)))
    try:
        with runtime.scope():
            return _run_pass(args)
    finally:
        _jsonl.append_row(os.path.join(paths.memory_dir(root), "dream_runs.jsonl"), runtime.manifest())


def _run_pass(args: argparse.Namespace) -> int:
    if args.recipe:
        print(recipe(args.recipe, args.every, args.dest), end="")
        return 0
    from commontrace.cli import main as cli

    root = paths.resolve_root(args.dest)
    started = time.monotonic()
    max_seconds = getattr(args, "max_seconds", 120)
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
            if time.monotonic() - started >= max_seconds:
                break
            from commontrace.llm_runtime import purpose

            with purpose("distill"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli(["distill", "--draft", "--signal", s.name, "--dest", root])
    elif signals:
        lines += ["_No model configured, so nothing was drafted. Run `commontrace distill --failed` for "
                  "evidence-only candidates._", ""]

    out = io.StringIO()
    from commontrace.llm_runtime import purpose

    with purpose("consolidate"), redirect_stdout(out), redirect_stderr(io.StringIO()):
        cli(["consolidate", *(["--draft"] if draft else []), "--dest", root])
    lines += ["## Consolidation", "", "```", out.getvalue().strip(), "```", ""]

    from commontrace import graph as graph_mod
    from commontrace import hierarchical, memory_blocks, store_state
    from commontrace.commands._traces import load_trace_instances

    linked_edges = 0
    with graph_mod.batch(root):
        for trace in load_trace_instances(root):
            if time.monotonic() - started >= max_seconds:
                break
            tid = str(trace.get("id") or "").strip()
            tags = trace.get("tags") if isinstance(trace.get("tags"), list) else []
            if not tid or not tags:
                continue
            graph_mod.add_node(root, f"trace:{tid}", "memory", name=str(trace.get("title") or tid)[:200])
            for tag in tags:
                tag = str(tag).strip()
                if not tag:
                    continue
                graph_mod.add_node(root, f"concept:{tag}", "concept", name=tag)
                graph_mod.add_edge(root, f"concept:{tag}", f"trace:{tid}", "affects")
                linked_edges += 1

    blocks = memory_blocks.list_blocks(root)
    facts = hierarchical.list_facts(root, status="active")
    status = store_state.inspect(root)

    lines += ["## Knowledge Graph & Cognitive Consolidation", ""]
    lines += [
        f"- Working memory blocks: {len(blocks)} active",
        f"- Atomic facts: {len(facts)} active",
        f"- Mined graph edges: {linked_edges} updated",
        "",
    ]

    profile_lines = [
        f"# CommonTrace Active Space Profile ({now.strftime('%Y-%m-%d %H:%M')}Z)", "",
        "## Working Memory Blocks",
    ]
    if blocks:
        for b in blocks:
            profile_lines.append(f"### [{b.name}] ({b.char_count}/{b.max_chars} chars, rev: {b.revision})")
            profile_lines.append(b.content)
            profile_lines.append("")
    else:
        profile_lines.append("_No working memory blocks configured._\n")

    profile_lines.append("## Key Atomic Truths")
    if facts:
        for f in facts[:15]:
            scope_str = f" [{','.join(f.scopes)}]" if f.scopes else ""
            profile_lines.append(f"- **{f.statement}** (conf: {f.confidence:.2f}){scope_str}")
        profile_lines.append("")
    else:
        profile_lines.append("_No atomic facts consolidated yet._\n")

    profile_lines.append(
        f"## Fleet Health\n- Active Lessons: {status.active}\n"
        f"- In Review: {status.review}\n- Total Traces: {status.traces}\n"
    )

    profile_path = os.path.join(paths.memory_dir(root), "profile.md")
    with open(profile_path, "w", encoding="utf-8", newline="\n") as pf:
        pf.write("\n".join(profile_lines) + "\n")

    lines += ["## Active Space Profile", f"- Synthesized to `{profile_path}`", ""]
    from commontrace import memory_control, observations

    if time.monotonic() - started < max_seconds:
        consolidated = observations.consolidate_facts(root)
        offline = memory_control.offline_pass(root, max_jobs=20,
                    seconds=max(0, max_seconds - (time.monotonic() - started)))
        lines += ["## Standing questions and observations",
                  f"- Observations consolidated: {len(consolidated)}", f"- Model refresh jobs: {offline}", ""]
    if time.monotonic() - started < max_seconds:
        from commontrace import experience_skills

        skills = experience_skills.cluster_agent_cases(root)
        lines += ["## Skills", f"- Evidence-linked skill proposals: {len(skills)}", ""]

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
    print(
        f"[commontrace] dream: {len(signals)} signal(s), {len(new)} new draft(s), "
        f"{len(after)} awaiting review, profile synthesized. "
        f"Report: {report}" + ("" if draft else " (no model: report only)")
    )
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


def main_dream(root: str, draft: bool = False) -> int:
    """Run a dreaming pass programmatically (used by agent_loop and tests)."""
    import argparse
    args = argparse.Namespace(
        recipe=None,
        every="weekly",
        dest=root,
        no_draft=not draft,
    )
    return run(args)

