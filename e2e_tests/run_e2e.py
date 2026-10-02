#!/usr/bin/env python3
"""CommonTrace v2 Master E2E Test Runner

Executes the 4-tier opaque-box E2E test suite and outputs structured results.
Usage:
    python3 e2e_tests/run_e2e.py
    python3 e2e_tests/run_e2e.py --tier 1
    python3 e2e_tests/run_e2e.py --tier 1,2
    python3 e2e_tests/run_e2e.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TIER_DIRS = {
    1: ("Tier 1: Feature Coverage", os.path.join(REPO_ROOT, "e2e_tests", "tier1_features")),
    2: ("Tier 2: Boundary & Corner Conditions", os.path.join(REPO_ROOT, "e2e_tests", "tier2_boundaries")),
    3: ("Tier 3: Cross-Feature Combinations", os.path.join(REPO_ROOT, "e2e_tests", "tier3_combinations")),
    4: ("Tier 4: Real-World Scenarios", os.path.join(REPO_ROOT, "e2e_tests", "tier4_real_world")),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CommonTrace v2 Master E2E Test Runner")
    parser.add_argument(
        "--tier",
        default="all",
        help="Tier(s) to run: 1, 2, 3, 4, 'all', or comma-separated list (e.g. '1,2'). Default: all",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose pytest output")
    parser.add_argument("-k", "--keyword", default="", help="Filter tests by keyword expression")
    parser.add_argument("-x", "--fail-fast", action="store_true", help="Stop on first failure")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON summary")
    return parser.parse_args()


def run_tier(tier_num: int, name: str, path: str, extra_args: list[str]) -> dict:
    start_time = time.time()
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO_ROOT}:{env.get('PYTHONPATH', '')}"

    cmd = [sys.executable, "-m", "pytest", path]
    cmd.extend(extra_args)

    proc = subprocess.run(cmd, env=env, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    duration = time.time() - start_time

    # Parse counts from pytest output
    passed = 0
    skipped = 0
    failed = 0
    errors = 0

    stdout_lines = proc.stdout.splitlines()
    for line in reversed(stdout_lines):
        line_clean = line.strip("= ")
        if "passed" in line_clean or "failed" in line_clean or "skipped" in line_clean:
            parts = [p.strip() for p in line_clean.split(",")]
            for part in parts:
                tokens = part.split()
                if len(tokens) >= 2:
                    try:
                        count = int(tokens[0])
                        stat_name = tokens[1]
                        if "pass" in stat_name:
                            passed = count
                        elif "skip" in stat_name:
                            skipped = count
                        elif "fail" in stat_name:
                            failed = count
                        elif "error" in stat_name:
                            errors = count
                    except ValueError:
                        pass
            break

    total = passed + skipped + failed + errors

    return {
        "tier": tier_num,
        "name": name,
        "path": path,
        "returncode": proc.returncode,
        "success": proc.returncode == 0,
        "total": total,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "duration_sec": round(duration, 2),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def main() -> int:
    args = parse_args()

    selected_tiers = []
    if args.tier.lower() == "all":
        selected_tiers = [1, 2, 3, 4]
    else:
        for t in args.tier.split(","):
            t = t.strip()
            if t.isdigit() and int(t) in TIER_DIRS:
                selected_tiers.append(int(t))
            else:
                print(f"Error: Unknown tier {t!r}. Must be 1, 2, 3, 4, or 'all'.", file=sys.stderr)
                return 2

    extra_pytest_args = []
    if args.verbose:
        extra_pytest_args.append("-v")
    if args.keyword:
        extra_pytest_args.extend(["-k", args.keyword])
    if args.fail_fast:
        extra_pytest_args.append("-x")

    tier_results = []
    overall_success = True

    if not args.json:
        print("=" * 80)
        print("  CommonTrace v2 Enterprise Cognitive Memory — Master E2E Suite")
        print("=" * 80)
        print(f"Python: {sys.version.split()[0]} | Root: {REPO_ROOT}")
        print(f"Selected Tiers: {selected_tiers}")
        print("-" * 80)

    for tier_num in selected_tiers:
        name, path = TIER_DIRS[tier_num]
        if not args.json:
            print(f"[*] Running {name} ({os.path.relpath(path, REPO_ROOT)})...", end="", flush=True)

        res = run_tier(tier_num, name, path, extra_pytest_args)
        tier_results.append(res)

        if not res["success"]:
            overall_success = False
            if not args.json:
                print(f" [FAILED] (exit code {res['returncode']})")
                if res["stderr"]:
                    print(res["stderr"])
            if args.fail_fast:
                break
        else:
            if not args.json:
                print(f" [PASSED] ({res['passed']} passed, {res['skipped']} skipped in {res['duration_sec']}s)")

    if args.json:
        output_payload = {
            "overall_success": overall_success,
            "total_passed": sum(r["passed"] for r in tier_results),
            "total_skipped": sum(r["skipped"] for r in tier_results),
            "total_failed": sum(r["failed"] for r in tier_results),
            "total_duration_sec": round(sum(r["duration_sec"] for r in tier_results), 2),
            "tiers": [
                {
                    "tier": r["tier"],
                    "name": r["name"],
                    "success": r["success"],
                    "total": r["total"],
                    "passed": r["passed"],
                    "skipped": r["skipped"],
                    "failed": r["failed"],
                    "duration_sec": r["duration_sec"],
                }
                for r in tier_results
            ],
        }
        print(json.dumps(output_payload, indent=2))
        return 0 if overall_success else 1

    print("=" * 80)
    print("E2E Execution Summary:")
    print("-" * 80)
    header = f"{'Tier':<40} {'Passed':<8} {'Skipped':<9} {'Failed':<8} {'Time':<8} {'Status'}"
    print(header)
    print("-" * 80)
    for r in tier_results:
        status_str = "PASS" if r["success"] else "FAIL"
        print(
            f"{r['name']:<40} {r['passed']:<8} {r['skipped']:<9} {r['failed']:<8} "
            f"{r['duration_sec']}s{'':<4} {status_str}"
        )
    print("-" * 80)
    total_passed = sum(r["passed"] for r in tier_results)
    total_skipped = sum(r["skipped"] for r in tier_results)
    total_failed = sum(r["failed"] for r in tier_results)
    total_time = round(sum(r["duration_sec"] for r in tier_results), 2)
    print(
        f"{'TOTALS':<40} {total_passed:<8} {total_skipped:<9} {total_failed:<8} "
        f"{total_time}s{'':<4} {'PASS' if overall_success else 'FAIL'}"
    )
    print("=" * 80)

    if overall_success:
        print("[SUCCESS] All end-to-end tests succeeded cleanly without regressions.")
        return 0
    else:
        print("[FAILURE] One or more test tiers experienced failures.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
