from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys

from commontrace import paths
from commontrace.commands._shellout import find_reference_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("doctor", help="Check the environment and store health.")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "WARN"
    line = f"[{mark}] {label}"
    if detail:
        line += f" - {detail}"
    print(line)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    print(f"[commontrace] doctor - store root: {root}\n")

    _check("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    _check("PyYAML importable", importlib.util.find_spec("yaml") is not None)
    _check("git on PATH", shutil.which("git") is not None)

    has_mem = os.path.isdir(paths.memory_dir(root))
    _check("memory/ store present", has_mem, paths.memory_dir(root) if has_mem else "run `commontrace init`")

    if has_mem:
        n_lessons = len(
            [
                f
                for f in os.listdir(paths.lessons_dir(root))
                if f.startswith("lesson_") and f != "lesson_template.md"
            ]
        ) if os.path.isdir(paths.lessons_dir(root)) else 0
        _check("lessons in store", n_lessons > 0, f"{n_lessons} found")

    attention_extra = importlib.util.find_spec("numpy") is not None and importlib.util.find_spec(
        "sentence_transformers"
    ) is not None
    _check(
        "attention extra installed (numpy + sentence-transformers)",
        attention_extra,
        "optional; install with `pip install commontrace[attention]`" if not attention_extra else "",
    )

    query_script = find_reference_script(root, "memory/attention/query.py")
    _check("reference attention/query.py found", query_script is not None, query_script or "not in a repo checkout")

    bench_script = find_reference_script(root, "benchmark/measure_performance.py")
    _check("reference benchmark script found", bench_script is not None, bench_script or "not in a repo checkout")

    protocol_dir = os.path.join(root, "protocol")
    _check("protocol/ spec present", os.path.isdir(protocol_dir), protocol_dir if os.path.isdir(protocol_dir) else "")

    print("\nDone.")
    return 0
