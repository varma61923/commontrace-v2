"""Bound GitHub Action inputs and dispatch real CLI commands without a shell."""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    workspace = os.path.realpath(os.environ["GITHUB_WORKSPACE"])
    value = os.environ.get("UNIMEM_ACTION_STORE", ".")
    root = os.path.realpath(os.path.join(workspace, value))
    if os.path.commonpath((workspace, root)) != workspace:
        raise ValueError("store must remain inside GITHUB_WORKSPACE, including symlinks")
    initialize = os.environ.get("UNIMEM_ACTION_INITIALIZE", "false")
    if initialize not in ("true", "false"):
        raise ValueError("initialize must be true or false")
    if initialize == "true":
        subprocess.run([sys.executable, "-m", "unimem", "init", "--dest", root], check=True, timeout=60)
    return subprocess.run([sys.executable, "-m", "unimem", "doctor", "--dest", root], timeout=60).returncode


if __name__ == "__main__":
    raise SystemExit(main())
