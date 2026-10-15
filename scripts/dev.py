"""Dependency-free development tasks."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=("test", "lint", "check", "serve"))
    parser.add_argument("--hub", action="store_true", help="run PostgreSQL Hub tests (disposable database required)")
    parser.add_argument("--dest", help="gateway store root for serve")
    args, extra = parser.parse_known_args()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if args.task == "test":
        if args.hub and not os.environ.get("HUB_TEST_DATABASE_URL"):
            parser.error("--hub requires HUB_TEST_DATABASE_URL pointing at a disposable database")
        command = [
            sys.executable,
            "-m",
            "pytest",
            *(["hub/tests"] if args.hub else ["tests", "e2e_tests"]),
            "-p",
            "no:cacheprovider",
            *extra,
        ]
    elif args.task == "lint":
        command = [sys.executable, "-m", "ruff", "check", ".", "--no-cache", *extra]
    elif args.task == "check":
        command = ["git", "diff", "--check", *extra]
    else:
        command = [
            sys.executable,
            "-m",
            "commontrace",
            "gateway",
            "serve",
            *(["--dest", args.dest] if args.dest else []),
            *extra,
        ]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
