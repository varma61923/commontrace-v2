"""Verify the installed local CLI can store and recall a fact without a model or server."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATEMENT = "The launch checklist is stored in the blue folder."
QUESTION = "Where is the launch checklist stored?"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--temp-dir",
        help="Parent directory for the disposable store (defaults to the system temp directory).",
    )
    args = parser.parse_args()
    cli = shutil.which("commontrace")
    if not cli:
        print("commontrace is not on PATH; install this checkout with `python -m pip install -e .` first.",
              file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["COMMONTRACE_OFFLINE"] = "1"
    timings: dict[str, float] = {}

    def run(label: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        started = time.perf_counter()
        result = subprocess.run([cli, *arguments], capture_output=True, text=True, env=env, check=False)
        timings[label] = round(time.perf_counter() - started, 3)
        if result.returncode:
            if result.stdout:
                print(result.stdout, file=sys.stderr, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            raise RuntimeError(f"commontrace {arguments[0]} failed with exit code {result.returncode}")
        return result

    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="commontrace-first-recall-", dir=args.temp_dir) as temporary:
            store = str(Path(temporary) / "store")
            run("init_s", ["init", "--dest", store])
            run("add_s", ["fact", "add", STATEMENT, "--dest", store])
            recalled = run("recall_s", ["recall", QUESTION, "--dest", store, "--json"])
            payload = json.loads(recalled.stdout)
            recalled_text = "\n".join(item.get("text", "") for item in payload.get("items", []))
            if STATEMENT.casefold() not in recalled_text.casefold():
                raise RuntimeError(f"recall did not return the seeded fact: {recalled_text!r}")
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"first-recall smoke failed: {exc}", file=sys.stderr)
        return 1

    timings["first_recall_s"] = round(time.perf_counter() - started, 3)
    print(json.dumps({"ok": True, "offline": True, "returned": STATEMENT, "timings": timings}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
