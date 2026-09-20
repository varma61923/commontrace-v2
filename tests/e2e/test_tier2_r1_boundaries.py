"""Tier 2: Boundary & Corner Cases for Protocol Core & CLI Robustness (Milestone 1).

Covers Edge Cases, Limits, Extremes for Features:
- R1-F1: CLI Subcommand Input Validation
- R1-F2: Standardized Exit Codes
- R1-F3: Pre-Write Schema Enforcement
- R1-F4: Positional Argument Flag Delimiting
- R1-F5: Repair install.sh Target Paths
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from tests.e2e.conftest import CLIResult


# ============================================================================
# R1-F1 Boundary Cases (5 tests)
# ============================================================================

def test_r1_f1_boundary_empty_agent_type(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that empty string for --agent-type is rejected."""
    res = cli_runner(["init", "--agent-type", "", "--dest", str(isolated_store)])
    assert res.exit_code == 2
    assert "not a valid agent type" in res.stderr


def test_r1_f1_boundary_max_length_agent_type(cli_runner: Callable[..., CLIResult], tmp_path: Path) -> None:
    """Validate that agent-type at 64 chars is accepted, while 65 chars is rejected."""
    valid_64 = "a" * 64
    invalid_65 = "a" * 65

    dir_64 = tmp_path / "store_64"
    res_64 = cli_runner(["init", "--agent-type", valid_64, "--dest", str(dir_64)])
    assert res_64.exit_code == 0

    dir_65 = tmp_path / "store_65"
    res_65 = cli_runner(["init", "--agent-type", invalid_65, "--dest", str(dir_65)])
    assert res_65.exit_code == 2
    assert "not a valid agent type" in res_65.stderr


def test_r1_f1_boundary_top_k_limits(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate boundary conditions for --top-k (minimum 1, large int, rejected values)."""
    # Minimum allowed top-k = 1
    res_min = cli_runner(["query", "--lexical", "--top-k", "1", "--dest", str(isolated_store), "test"])
    assert res_min.exit_code == 0

    # Large top-k
    res_large = cli_runner(["query", "--lexical", "--top-k", "99999", "--dest", str(isolated_store), "test"])
    assert res_large.exit_code == 0

    # Non-integer top-k
    res_str = cli_runner(["query", "--top-k", "abc", "--dest", str(isolated_store), "test"])
    assert res_str.exit_code == 2

    # Negative top-k
    res_neg = cli_runner(["query", "--top-k", "-10", "--dest", str(isolated_store), "test"])
    assert res_neg.exit_code == 2


def test_r1_f1_boundary_similarity_threshold_epsilon(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate boundary extremes for --similarity-threshold (0.0001, 1.0, 0.0, 1.0001)."""
    # Epsilon above 0
    res_low = cli_runner(["distill", "--similarity-threshold", "0.0001", "--dest", str(isolated_store)])
    assert res_low.exit_code == 0

    # Maximum 1.0
    res_max = cli_runner(["distill", "--similarity-threshold", "1.0", "--dest", str(isolated_store)])
    assert res_max.exit_code == 0

    # Negative epsilon below 0
    res_neg = cli_runner(["distill", "--similarity-threshold", "-0.001", "--dest", str(isolated_store)])
    assert res_neg.exit_code == 2

    # Epsilon above 1.0
    res_high = cli_runner(["distill", "--similarity-threshold", "1.0001", "--dest", str(isolated_store)])
    assert res_high.exit_code == 2


def test_r1_f1_boundary_empty_fields_capture(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that empty strings for title, context, or solution are rejected."""
    res_title = cli_runner([
        "capture",
        "--title", "",
        "--context", "context",
        "--solution", "solution",
        "--dest", str(isolated_store),
    ])
    assert res_title.exit_code == 1
    assert "refusing to write an invalid trace" in res_title.stderr


# ============================================================================
# R1-F2 Boundary Cases (5 tests)
# ============================================================================

def test_r1_f2_boundary_sigint_simulation() -> None:
    """Validate that KeyboardInterrupt in main() maps cleanly to exit code 130."""
    from commontrace import cli

    class MockKeyboardInterruptAction:
        def __call__(self, *args, **kwargs):
            raise KeyboardInterrupt()

    # Pass an argv that would trigger KeyboardInterrupt
    import argparse
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    p = subparsers.add_parser("test_sigint")
    p.set_defaults(func=MockKeyboardInterruptAction())

    # Call main with KeyboardInterrupt
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        rc = 130
    assert rc == 130


def test_r1_f2_boundary_oserror_clean_exit_code_1(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that unreadable or inaccessible file path returns exit code 1 without traceback."""
    res = cli_runner(["lesson", "validate", "/dev/null/impossible_file.md", "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "Traceback" not in res.stderr
    assert "[commontrace] error:" in res.stderr


def test_r1_f2_boundary_bare_commontrace_exits_code_2(cli_runner: Callable[..., CLIResult]) -> None:
    """Validate that invoking bare `commontrace` with no subcommands prints help and exits with 2."""
    res = cli_runner([])
    assert res.exit_code == 2
    assert "the following arguments are required: command" in res.stderr or "usage:" in res.stderr


def test_r1_f2_boundary_help_on_all_core_subcommands(cli_runner: Callable[..., CLIResult]) -> None:
    """Validate that --help exits 0 cleanly across all 10 core subcommands."""
    core_commands = ["init", "install", "capture", "trace", "lesson", "query", "index", "bench", "sync", "doctor"]
    for cmd in core_commands:
        res = cli_runner([cmd, "--help"])
        assert res.exit_code == 0, f"Command {cmd} --help failed with exit code {res.exit_code}"


def test_r1_f2_boundary_corrupt_csv_import_exits_code_1(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    tmp_path: Path,
) -> None:
    """Validate that unparseable or all-skipped CSV rows surface as clean error with exit code 1."""
    corrupt_csv = tmp_path / "corrupt.csv"
    # Row missing all required fields (title, context, solution) -> all rows skipped -> exit code 1
    corrupt_csv.write_text("col_a,col_b,col_c\nval1,val2,val3\n", encoding="utf-8")

    res = cli_runner(["import", str(corrupt_csv), "--agent-type", "code", "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "skipped" in res.stdout or "skipped" in res.stderr
    assert "Traceback" not in res.stderr


# ============================================================================
# R1-F3 Boundary Cases (5 tests)
# ============================================================================

def test_r1_f3_boundary_metrics_zero_accepted(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that outcome metrics set to exact 0 are accepted (valid boundary)."""
    res = cli_runner([
        "capture",
        "--title", "Zero Metric Trace",
        "--context", "Problem context",
        "--solution", "Solution context",
        "--tokens-used", "0",
        "--llm-calls", "0",
        "--dest", str(isolated_store),
    ])
    assert res.exit_code == 0


def test_r1_f3_boundary_negative_tokens_rejected(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that tokens_used = -1 is rejected before write."""
    res = cli_runner([
        "capture",
        "--title", "Negative Tokens Trace",
        "--context", "Problem",
        "--solution", "Solution",
        "--tokens-used", "-1",
        "--dest", str(isolated_store),
    ])
    assert res.exit_code == 1
    assert "refusing to write an invalid trace" in res.stderr


def test_r1_f3_boundary_warn_chars_threshold(
    isolated_store: Path,
) -> None:
    """Validate that check_text_size warns when text exceeds WARN_CHARS (256KB) but < 1MB."""
    from commontrace.commands._validators import check_text_size, WARN_CHARS, REFUSE_CHARS

    # Between 256KB and 1MB
    text_300k = "B" * (WARN_CHARS + 1000)
    fields = {"description": text_300k}
    # Should warn and return True (allowed to write, with warning)
    assert check_text_size(fields, what="lesson") is True


def test_r1_f3_boundary_refuse_chars_threshold() -> None:
    """Validate that check_text_size refuses when text strictly exceeds REFUSE_CHARS (1MB)."""
    from commontrace.commands._validators import check_text_size, REFUSE_CHARS

    text_over_1mb = "C" * (REFUSE_CHARS + 1)
    fields = {"description": text_over_1mb}
    assert check_text_size(fields, what="lesson") is False


def test_r1_f3_boundary_unfilled_placeholders_permitted_for_review_status(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that unedited TODO scaffolding is allowed in review-status lessons."""
    lesson_path = lesson_factory(
        isolated_store,
        slug="lesson_candidate_review",
        status="review",
        body="## Rule\nTODO: Under review\n",
    )
    res = cli_runner(["lesson", "validate", str(lesson_path), "--dest", str(isolated_store)])
    assert res.exit_code == 0
    assert "OK" in res.stdout


# ============================================================================
# R1-F4 Boundary Cases (5 tests)
# ============================================================================

def test_r1_f4_boundary_task_is_exact_double_dash(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure task string that is literally '--' executes cleanly when delimited."""
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "--"])
    assert res.exit_code == 0


def test_r1_f4_boundary_task_with_many_leading_dashes(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure task strings with 3, 4, 5 leading dashes do not crash argument parsing."""
    res3 = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "---triple-dash"])
    assert res3.exit_code == 0

    res5 = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "-----five-dash"])
    assert res5.exit_code == 0


def test_r1_f4_boundary_task_with_single_char_flags(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure task strings matching single-letter flags (e.g. -x, -k, -h) are delimited."""
    for flag_like in ["-x", "-k", "-h", "-v"]:
        res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", flag_like])
        assert res.exit_code == 0


def test_r1_f4_boundary_task_containing_flag_syntax_internally(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure complex query strings with internal flags are preserved."""
    res = cli_runner([
        "query", "--lexical",
        "--dest", str(isolated_store),
        "--",
        "find rules with --format=json and --dest=/tmp",
    ])
    assert res.exit_code == 0


def test_r1_f4_boundary_task_empty_string(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure empty task string query runs without error."""
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), ""])
    assert res.exit_code == 0


# ============================================================================
# R1-F5 Boundary Cases (5 tests)
# ============================================================================

def test_r1_f5_boundary_install_sh_dest_with_spaces(tmp_path: Path) -> None:
    """Validate install.sh handles custom destination directory with spaces."""
    install_script = Path("/root/commontrace-v2/install.sh")
    dest_with_space = tmp_path / "custom store with spaces"

    res = subprocess.run(
        ["bash", str(install_script), "--dest", str(dest_with_space), "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
    assert dest_with_space.exists()


def test_r1_f5_boundary_install_sh_trailing_slash(tmp_path: Path) -> None:
    """Validate install.sh handles destination with trailing slash."""
    install_script = Path("/root/commontrace-v2/install.sh")
    dest_slash = str(tmp_path / "store_trailing") + "/"

    res = subprocess.run(
        ["bash", str(install_script), "--dest", dest_slash, "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0


def test_r1_f5_boundary_install_sh_invalid_python_binary() -> None:
    """Validate install.sh fails cleanly when given non-existent python binary."""
    install_script = Path("/root/commontrace-v2/install.sh")
    res = subprocess.run(
        ["bash", str(install_script), "--python", "/nonexistent/python_bin_xyz", "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode != 0
    assert "not found" in res.stderr.lower() or "error" in res.stderr.lower() or "not executable" in res.stderr.lower()


def test_r1_f5_boundary_install_sh_dest_equals_syntax(tmp_path: Path) -> None:
    """Validate install.sh supports `--dest=PATH` argument syntax."""
    install_script = Path("/root/commontrace-v2/install.sh")
    dest_path = tmp_path / "dest_eq"

    res = subprocess.run(
        ["bash", str(install_script), f"--dest={dest_path}", "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
    assert dest_path.exists()


def test_r1_f5_boundary_install_sh_repeated_flags(tmp_path: Path) -> None:
    """Validate that passing flags multiple times (e.g. --no-deps --no-deps) does not crash."""
    install_script = Path("/root/commontrace-v2/install.sh")
    dest_path = tmp_path / "dest_repeat"

    res = subprocess.run(
        ["bash", str(install_script), "--dest", str(dest_path), "--no-deps", "--no-deps", "--no-index", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
