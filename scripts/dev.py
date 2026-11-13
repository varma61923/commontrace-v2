"""Dependency-free development tasks."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLEAN_PATHS = (
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "coverage.xml",
    "htmlcov",
    "build",
    "dist",
    "sdk/typescript/dist",
    "sdk/rust/target",
    "sdk/jvm/target",
    "sdk/generated",
    "distribution/jetbrains/.gradle",
    "distribution/jetbrains/.intellijPlatform",
    "distribution/jetbrains/build",
    "ros2/build",
    "ros2/install",
    "ros2/log",
    "zk/target",
    "zk/methods/guest/target",
)


def clean(*, dry_run: bool = False) -> int:
    """Remove ignored build/cache outputs while preserving source and local state."""
    ignored = subprocess.check_output(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"], cwd=ROOT
    )
    paths = set(CLEAN_PATHS)
    source_roots = {
        "benchmarks",
        "commons",
        "distribution",
        "e2e_tests",
        "hub",
        "metrics",
        "scripts",
        "tests",
        "unimem",
    }
    for name in ignored.split(b"\0"):
        if not name:
            continue
        path = Path(os.fsdecode(name))
        if len(path.parts) == 1 and path.name.startswith(".coverage."):
            paths.add(path.as_posix())
        elif (len(path.parts) == 1 or path.parts[0] in source_roots) and (
            path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
        ):
            paths.add(path.as_posix())
    # Literal, explicit paths prevent a bytecode glob from matching an entire
    # ignored virtualenv or node_modules directory containing a matching child.
    return subprocess.run(
        ["git", "clean", "-ndX" if dry_run else "-fdX", "--", *(f":(literal){p}" for p in sorted(paths))],
        cwd=ROOT,
        check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=("test", "lint", "check", "serve", "clean"))
    parser.add_argument("--hub", action="store_true", help="run PostgreSQL Hub tests (disposable database required)")
    parser.add_argument("--dest", help="gateway store root for serve")
    args, extra = parser.parse_known_args()
    if args.task == "clean":
        if extra not in ([], ["--dry-run"]):
            parser.error("clean only accepts --dry-run")
        return clean(dry_run=bool(extra))
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
        ruff_bin = shutil.which("ruff") or sys.executable
        if ruff_bin != sys.executable:
            check_command = [ruff_bin, "check", ".", "--no-cache", *extra]
            command = [ruff_bin, "format", "--check", ".", "--exclude", "hub/alembic/versions/**", *extra]
        else:
            check_command = [sys.executable, "-m", "ruff", "check", ".", "--no-cache", *extra]
            command = [
                sys.executable,
                "-m",
                "ruff",
                "format",
                "--check",
                ".",
                "--exclude",
                "hub/alembic/versions/**",
                *extra,
            ]
        checked = subprocess.run(check_command, cwd=ROOT, env=env, check=False)
        if checked.returncode:
            return checked.returncode
    elif args.task == "check":
        command = ["git", "diff", "--check", *extra]
    else:
        command = [
            sys.executable,
            "-m",
            "unimem",
            "gateway",
            "serve",
            *(["--dest", args.dest] if args.dest else []),
            *extra,
        ]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
