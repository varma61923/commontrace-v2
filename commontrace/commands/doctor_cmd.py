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


# Accumulates failed checks so run() can exit non-zero. Module-level rather
# than threaded through every call site because _check is used a dozen times
# and the alternative is a parameter on each; run() resets it on entry so
# repeated in-process invocations (tests) do not inherit stale state.
_FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "", critical: bool = False) -> None:
    """`critical=True` means the tool cannot function, and only those affect
    the exit code.

    The distinction is load-bearing. A freshly `init`-ed store legitimately
    has zero lessons and a pip-installed client legitimately has no
    benchmark script -- both print [WARN] because they are worth seeing, and
    neither is a failure. Exiting non-zero for them would make day one of
    every install look broken to CI, which is how a health check gets
    ignored and then stops being read at all.
    """
    mark = "OK  " if ok else "WARN"
    line = f"[{mark}] {label}"
    if detail:
        line += f" - {detail}"
    print(line)
    if not ok and critical:
        _FAILURES.append(label)


def _info(label: str, detail: str = "") -> None:
    """For conditions that are expected/normal in a standard client install and need no
    action -- as opposed to _check(..., ok=False), which means something is actually wrong
    and worth fixing. Keeping these off [WARN] means a clean `pip install commontrace` +
    `commontrace init` install doesn't read as having problems it doesn't have."""
    line = "[INFO] " + label
    if detail:
        line += f" - {detail}"
    print(line)


def run(args: argparse.Namespace) -> int:
    _FAILURES.clear()
    root = paths.resolve_root(args.dest)
    print(f"[commontrace] doctor - store root: {root}\n")

    _check("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0], critical=True)
    _check("PyYAML importable", importlib.util.find_spec("yaml") is not None, critical=True)
    _check("git on PATH", shutil.which("git") is not None)

    has_mem = os.path.isdir(paths.memory_dir(root))
    _check("memory/ store present", has_mem,
           paths.memory_dir(root) if has_mem else "run `commontrace init`", critical=True)

    if has_mem:
        n_lessons = 0
        ldir = paths.lessons_dir(root)
        if os.path.isdir(ldir):
            try:
                n_lessons = len(
                    [
                        f
                        for f in os.listdir(ldir)
                        if f.startswith("lesson_") and f != "lesson_template.md"
                    ]
                )
            except OSError:
                n_lessons = 0
        _check("lessons in store", n_lessons > 0, f"{n_lessons} found")

    attention_extra = importlib.util.find_spec("numpy") is not None and importlib.util.find_spec(
        "sentence_transformers"
    ) is not None
    if attention_extra:
        _check("attention extra installed (numpy + sentence-transformers)", True)
    else:
        _info(
            "attention extra installed (numpy + sentence-transformers)",
            "optional; install with `pip install commontrace[attention]` for semantic retrieval",
        )

    query_script = find_reference_script(root, "memory/attention/query.py")
    if query_script is not None:
        _check("reference attention/query.py found", True, query_script)
    else:
        _info("reference attention/query.py found", "not in a repo checkout (expected for a pip-installed client)")

    bench_script = find_reference_script(root, "benchmark/measure_performance.py")
    if bench_script is not None:
        _check("benchmark script found", True, bench_script)
    else:
        # It ships inside the package now, so absence means a damaged install
        # rather than "you are not in a repo checkout".
        _check("benchmark script found", False,
               "missing from the installed package - try `pip install --force-reinstall commontrace`")

    protocol_dir = os.path.join(root, "protocol")
    if os.path.isdir(protocol_dir):
        _check("protocol/ spec present", True, protocol_dir)
    else:
        _info(
            "protocol/ spec present",
            "not in a repo checkout; schemas are mirrored at commontrace/schemas/ for the installed package",
        )

    if _FAILURES:
        # Non-zero so a CI gate, a container health check, or an onboarding
        # script can act on this. Returning 0 unconditionally meant `doctor`
        # could report a missing store, no lessons and an unsupported Python
        # and still look like a pass to everything except a human reading
        # the output. _info conditions are deliberately excluded -- they are
        # normal for a clean client install and must not fail a pipeline.
        print(f"\nDone. {len(_FAILURES)} critical check(s) failed: "
              + ", ".join(_FAILURES))
        return 1

    print("\nDone.")
    return 0
