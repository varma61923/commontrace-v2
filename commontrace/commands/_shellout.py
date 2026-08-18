from __future__ import annotations

import os
import subprocess
import sys

from commontrace import paths


def find_reference_script(root: str, relative: str) -> str | None:
    """Locate a reference-implementation script (attention/query.py, benchmark/...).

    These live in the commontrace-v2 repo checkout, not in the pip package
    (they carry heavier optional dependencies — see pyproject.toml's
    `attention` extra). A pure `pip install commontrace` without a repo
    checkout nearby won't have them; callers should fail with a clear
    message rather than a traceback.
    """
    candidates = [
        os.path.join(root, relative),
        os.path.join(os.getcwd(), relative),
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
    env.setdefault("COMMONTRACE_ROOT", root)
    result = subprocess.run([sys.executable, script, *extra_args], env=env)
    return result.returncode
