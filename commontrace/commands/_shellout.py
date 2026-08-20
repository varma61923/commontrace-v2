from __future__ import annotations

import importlib.util
import os
import subprocess
import sys


def has_attention_deps() -> bool:
    """Whether the optional semantic attention layer's deps (numpy, sentence-transformers)
    are importable. Check this before shelling out to memory/attention/*.py -- those scripts
    `import numpy` at module scope, so without this check a missing dep surfaces as a raw
    traceback (internal file paths and all) instead of the `pip install commontrace[attention]`
    hint `doctor` already gives.
    """
    return (
        importlib.util.find_spec("numpy") is not None
        and importlib.util.find_spec("sentence_transformers") is not None
    )


def packaged_reference_dir() -> str:
    """Where reference scripts that ship inside the wheel live.

    The benchmark scripts need nothing beyond PyYAML, so they are packaged
    (see pyproject.toml's package-data) and a plain `pip install commontrace`
    can run `commontrace bench` without a repo checkout. The attention
    scripts are not packaged -- they need numpy + sentence-transformers.
    """
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reference")


def find_reference_script(root: str, relative: str) -> str | None:
    """Locate a reference-implementation script (attention/query.py, benchmark/...).

    Checked in order: the store root, the working directory, then the copy
    bundled in the installed package. The first two let a repo checkout's
    edited copy win, which is what a contributor expects; the third is what
    makes the command work for someone who only ran `pip install commontrace`.

    Scripts with heavy optional dependencies (memory/attention/*, needing the
    `attention` extra) are deliberately not bundled, so they still resolve to
    None here and callers report the install hint rather than a traceback.
    """
    candidates = [
        os.path.join(root, relative),
        os.path.join(os.getcwd(), relative),
        os.path.join(packaged_reference_dir(), os.path.basename(relative)),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def run_script(root: str, relative: str, extra_args: list[str], missing_hint: str) -> int:
    script = find_reference_script(root, relative)
    if script is None:
        print(
            f"[commontrace] Could not find {relative}. {missing_hint}",
            file=sys.stderr,
        )
        return 1
    env = dict(os.environ)
    # Assign, never setdefault. `root` is already the resolved winner of
    # paths.resolve_root() -- --dest beats $COMMONTRACE_ROOT beats cwd. With
    # setdefault, an exported COMMONTRACE_ROOT survives into the child and
    # silently overrides an explicit --dest, inverting that documented
    # priority. The failure is invisible: `bench --pilot --dest B` renders a
    # normal-looking report full of store A's numbers.
    env["COMMONTRACE_ROOT"] = root
    result = subprocess.run([sys.executable, script, *extra_args], env=env)
    return result.returncode
