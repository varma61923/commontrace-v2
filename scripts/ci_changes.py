"""Print `code=true|false` for CI: false only when every changed path is documentation.

Unknown history (a new branch, a force push, a shallow clone) always means
`code=true`, so a skip can only happen when the diff is known to be docs-only.
"""
from __future__ import annotations

import re
import subprocess
import sys

DOCS_ONLY = re.compile(r"^(docs/.*|[^/]+\.md|LICENSE)$")
NULL_SHA = "0" * 40


def changed_files(base: str, head: str) -> list[str] | None:
    if not base or base == NULL_SHA:
        return None
    try:
        subprocess.run(["git", "cat-file", "-e", base + "^{commit}"], check=True, capture_output=True)
        out = subprocess.run(["git", "diff", "--name-only", base, head], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in out.stdout.splitlines() if line]


def code_changed(files: list[str] | None) -> bool:
    return files is None or not files or any(not DOCS_ONLY.match(path) for path in files)


def main(argv: list[str]) -> int:
    base, head = (argv + ["", "HEAD"])[:2]
    print("code=" + ("true" if code_changed(changed_files(base, head or "HEAD")) else "false"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
