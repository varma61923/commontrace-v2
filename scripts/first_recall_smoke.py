"""Verify the installed local CLI can store and recall a fact without a model or server."""

from __future__ import annotations

import argparse
import json
import math
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
    parser.add_argument("--timeout", type=float, default=60, help="Total deadline in seconds for the complete journey.")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    cli = shutil.which("unimem")
    if not cli:
        print("unimem is not on PATH; install this checkout with `python -m pip install -e .` first.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["UNIMEM_OFFLINE"] = "1"
    timings: dict[str, float] = {}

    def run(label: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        started = time.perf_counter()
        remaining = args.timeout - (time.perf_counter() - started_all)
        if remaining <= 0:
            raise RuntimeError("first-recall deadline expired")
        result = subprocess.run(
            [cli, *arguments], capture_output=True, text=True, env=env, check=False, timeout=remaining
        )
        timings[label] = round(time.perf_counter() - started, 3)
        if result.returncode:
            if result.stdout:
                print(result.stdout, file=sys.stderr, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            raise RuntimeError(f"unimem {arguments[0]} failed with exit code {result.returncode}")
        return result

    started_all = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="unimem-first-recall-", dir=args.temp_dir) as temporary:
            store = str(Path(temporary) / "store")
            run("init_s", ["init", "--dest", store])
            run("add_s", ["fact", "add", STATEMENT, "--dest", store])
            recalled = run("recall_s", ["recall", QUESTION, "--dest", store, "--json"])
            payload = json.loads(recalled.stdout)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise RuntimeError("recall did not return an items array")
            if any(not isinstance(item, dict) or not isinstance(item.get("text"), str) for item in payload["items"]):
                raise RuntimeError("recall items must contain text strings")
            recalled_text = "\n".join(item["text"] for item in payload["items"])
            if STATEMENT.casefold() not in recalled_text.casefold():
                raise RuntimeError(f"recall did not return the seeded fact: {recalled_text!r}")
    except (OSError, json.JSONDecodeError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"first-recall smoke failed: {exc}", file=sys.stderr)
        return 1

    timings["first_recall_s"] = round(time.perf_counter() - started_all, 3)
    print(json.dumps({"ok": True, "offline": True, "returned": STATEMENT, "timings": timings}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
