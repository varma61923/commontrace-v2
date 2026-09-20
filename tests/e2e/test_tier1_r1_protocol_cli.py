"""Tier 1: Feature Coverage for Protocol Core & CLI Robustness (Milestone 1).

Covers Features:
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
# R1-F1: CLI Subcommand Input Validation (>=5 tests)
# ============================================================================

def test_r1_f1_init_validates_agent_type_slug(cli_runner: Callable[..., CLIResult], tmp_path: Path) -> None:
    """Validate that `commontrace init` enforces agent_type slug pattern."""
    target_dir = tmp_path / "init_test"
    # Invalid slug with uppercase and special characters should fail with exit code 2 (argparse)
    res_bad = cli_runner(["init", "--dest", str(target_dir), "--agent-type", "INVALID TYPE!"])
    assert res_bad.exit_code == 2
    assert "not a valid agent type" in res_bad.stderr

    # Valid slug should succeed with exit code 0
    res_good = cli_runner(["init", "--dest", str(target_dir), "--agent-type", "support_ops-1"])
    assert res_good.exit_code == 0
    assert "Initialized a support_ops-1 store" in res_good.stdout


def test_r1_f1_capture_validates_required_args(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that `commontrace capture` enforces required arguments (--title, --context, --solution)."""
    res = cli_runner(["capture", "--dest", str(isolated_store), "--title", "Missing Others"])
    assert res.exit_code == 2
    assert "the following arguments are required" in res.stderr
    assert "--context" in res.stderr or "--solution" in res.stderr


def test_r1_f1_query_validates_top_k_bounds(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that `commontrace query` enforces --top-k >= 1."""
    # Negative top-k
    res_neg = cli_runner(["query", "search term", "--top-k", "-5", "--dest", str(isolated_store)])
    assert res_neg.exit_code == 2
    assert "--top-k must be >= 1" in res_neg.stderr

    # Zero top-k
    res_zero = cli_runner(["query", "search term", "--top-k", "0", "--dest", str(isolated_store)])
    assert res_zero.exit_code == 2
    assert "--top-k must be >= 1" in res_zero.stderr


def test_r1_f1_distill_validates_similarity_threshold(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that `commontrace distill` rejects similarity thresholds outside (0, 1]."""
    res_zero = cli_runner(["distill", "--similarity-threshold", "0", "--dest", str(isolated_store)])
    assert res_zero.exit_code == 2
    assert "must be > 0 and <= 1" in res_zero.stderr

    res_too_high = cli_runner(["distill", "--similarity-threshold", "1.5", "--dest", str(isolated_store)])
    assert res_too_high.exit_code == 2
    assert "must be > 0 and <= 1" in res_too_high.stderr


def test_r1_f1_export_validates_kind_choices(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that `commontrace export` enforces --kind enum choices."""
    res = cli_runner(["export", "--kind", "unsupported_kind", "--dest", str(isolated_store)])
    assert res.exit_code == 2
    assert "invalid choice: 'unsupported_kind'" in res.stderr


def test_r1_f1_lesson_new_validates_slug_pattern(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that `commontrace lesson new` rejects slugs with path traversal or invalid characters."""
    res = cli_runner([
        "lesson", "new",
        "--slug", "../traversal_lesson",
        "--description", "desc",
        "--domain", "security",
        "--dest", str(isolated_store),
    ])
    assert res.exit_code in (1, 2)
    assert "invalid --slug" in res.stderr or "error" in res.stderr


# ============================================================================
# R1-F2: Standardized Exit Codes (>=5 tests)
# ============================================================================

def test_r1_f2_exit_code_0_on_success(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate exit code 0 on clean successful operations."""
    res_help = cli_runner(["--help"])
    assert res_help.exit_code == 0

    res_doctor = cli_runner(["doctor", "--dest", str(isolated_store)])
    assert res_doctor.exit_code == 0

    res_version = cli_runner(["--version"])
    assert res_version.exit_code == 0


def test_r1_f2_exit_code_1_on_operational_and_validation_error(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate exit code 1 on operational, schema, and validation failures."""
    res = cli_runner(["lesson", "validate", "/nonexistent/path.md", "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "error" in res.stderr.lower()


def test_r1_f2_exit_code_2_on_argparse_syntax_error(cli_runner: Callable[..., CLIResult]) -> None:
    """Validate exit code 2 on unrecognized command flags and invalid CLI syntax."""
    res = cli_runner(["init", "--nonexistent-unrecognized-flag"])
    assert res.exit_code == 2
    assert "unrecognized arguments" in res.stderr


def test_r1_f2_exit_code_2_on_missing_required_positional(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate exit code 2 when required positional argument is omitted."""
    res = cli_runner(["query"])
    assert res.exit_code == 2
    assert "the following arguments are required" in res.stderr


def test_r1_f2_clean_error_message_no_traceback_on_exit_1(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    """Validate that exit code 1 formats clean diagnostic without printing raw Python tracebacks."""
    res = cli_runner(["lesson", "validate", str(isolated_store / "memory"), "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "[commontrace] error:" in res.stderr
    assert "Traceback (most recent call last)" not in res.stderr


# ============================================================================
# R1-F3: Pre-Write Schema Enforcement (>=5 tests)
# ============================================================================

def test_r1_f3_capture_refuses_negative_tokens_used_pre_write(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `capture` rejects negative tokens_used before writing file to disk."""
    traces_dir = isolated_store / "memory" / "traces"
    existing_files = set(traces_dir.glob("*.md")) if traces_dir.exists() else set()

    res = cli_runner([
        "capture",
        "--title", "Test Negative Tokens",
        "--context", "Problem with negative tokens",
        "--solution", "Attempted fix",
        "--tokens-used", "-100",
        "--dest", str(isolated_store),
    ])
    assert res.exit_code == 1
    assert "refusing to write an invalid trace" in res.stderr

    new_files = set(traces_dir.glob("*.md")) if traces_dir.exists() else set()
    assert new_files == existing_files, "No trace file should be written when validation fails"


def test_r1_f3_capture_refuses_negative_llm_calls_pre_write(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `capture` rejects negative llm_calls before writing file to disk."""
    traces_dir = isolated_store / "memory" / "traces"
    existing_files = set(traces_dir.glob("*.md")) if traces_dir.exists() else set()

    res = cli_runner([
        "capture",
        "--title", "Test Negative LLM Calls",
        "--context", "Problem with negative calls",
        "--solution", "Attempted fix",
        "--llm-calls", "-1",
        "--dest", str(isolated_store),
    ])
    assert res.exit_code == 1
    assert "refusing to write an invalid trace" in res.stderr

    new_files = set(traces_dir.glob("*.md")) if traces_dir.exists() else set()
    assert new_files == existing_files


def test_r1_f3_capture_writes_valid_schema_and_validates(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that valid capture arguments write a file that cleanly passes schema validation."""
    res_cap = cli_runner([
        "capture",
        "--title", "Valid Trace Verification",
        "--context", "Valid problem context",
        "--solution", "Valid solution implemented",
        "--tokens-used", "500",
        "--llm-calls", "3",
        "--resolved",
        "--dest", str(isolated_store),
    ])
    assert res_cap.exit_code == 0
    assert "captured trace" in res_cap.output.lower()

    res_val = cli_runner(["trace", "validate", "--dest", str(isolated_store)])
    assert res_val.exit_code == 0
    assert "traces valid" in res_val.stdout


def test_r1_f3_lesson_new_refuses_oversized_payload(
    isolated_store: Path,
) -> None:
    """Validate that text size validator refuses writes exceeding REFUSE_CHARS (1MB)."""
    from commontrace.commands._validators import check_text_size, REFUSE_CHARS
    fields = {"description": "X" * (REFUSE_CHARS + 100), "applies_when": "always"}
    assert check_text_size(fields, what="lesson") is False



def test_r1_f3_lesson_validate_detects_unfilled_placeholders_in_active_lesson(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that `lesson validate` rejects active lessons containing TODO scaffolding."""
    lesson_path = lesson_factory(
        isolated_store,
        slug="lesson_scaffolded",
        status="active",
        body="## Rule\nTODO: Write actual rule details here\n",
    )
    res = cli_runner(["lesson", "validate", str(lesson_path), "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "unedited scaffolding" in res.stdout or "scaffolding" in res.stderr


# ============================================================================
# R1-F4: Positional Argument Flag Delimiting (>=5 tests)
# ============================================================================

def test_r1_f4_query_with_leading_double_dash_task(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Ensure task strings starting with '--' do not trigger option parsing errors."""
    lesson_factory(
        isolated_store,
        slug="lesson_refactor_guard",
        title="Refactor Safety",
        description="Guidance when running refactor steps",
        applies_when="When executing refactor tasks",
    )
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "--refactor steps"])
    assert res.exit_code == 0
    assert "lesson_refactor_guard" in res.stdout or "no match" in res.stdout


def test_r1_f4_query_with_single_dash_task(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure task strings starting with single dash (e.g. '-v') are parsed as positional text."""
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "-v verbose check"])
    assert res.exit_code == 0


def test_r1_f4_query_task_resembling_builtin_flag(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure '--help' or '--version' inside task string is treated as search query with '--' delimiter."""
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "--help needed for build"])
    assert res.exit_code == 0
    assert "usage: commontrace query" not in res.stdout, "Should not print usage help when delimited"


def test_r1_f4_query_with_spaces_and_quotes(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Ensure complex delimited task strings with quotes and special characters execute properly."""
    lesson_factory(
        isolated_store,
        slug="lesson_git_safety",
        title="Git Safety",
        description="Never force push to main branch",
        applies_when="Running git push commands",
    )
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "--git 'force-push' safety"])
    assert res.exit_code == 0


def test_r1_f4_query_hybrid_and_lexical_with_flag_delimited_task(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Ensure query handles leading dash strings without crashing across both dispatch pathways."""
    res_lex = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "--dry-run simulation"])
    assert res_lex.exit_code == 0

    res_def = cli_runner(["query", "--dest", str(isolated_store), "--", "--dry-run simulation"])
    assert res_def.exit_code == 0


# ============================================================================
# R1-F5: Repair install.sh Target Paths (>=5 tests)
# ============================================================================

def test_r1_f5_install_sh_help_exits_cleanly() -> None:
    """Validate that `./install.sh --help` executes cleanly and returns exit code 0."""
    install_script = Path("/root/commontrace-v2/install.sh")
    assert install_script.exists()
    res = subprocess.run(
        ["bash", str(install_script), "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
    assert "install.sh" in res.stdout


def test_r1_f5_install_sh_dest_without_arg_fails_cleanly() -> None:
    """Validate that `./install.sh --dest` without argument emits actionable error and exits 1."""
    install_script = Path("/root/commontrace-v2/install.sh")
    res = subprocess.run(
        ["bash", str(install_script), "--dest"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 1
    assert "--dest requires a path argument" in res.stderr


def test_r1_f5_install_sh_unknown_arg_fails_cleanly() -> None:
    """Validate that `./install.sh` rejects unknown flags with exit code 1."""
    install_script = Path("/root/commontrace-v2/install.sh")
    res = subprocess.run(
        ["bash", str(install_script), "--unrecognized-option-test"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 1
    assert "Unknown argument" in res.stderr


def test_r1_f5_install_sh_supports_in_place_flag() -> None:
    """Validate that `./install.sh --in-place --no-deps --no-index` runs without error in the repo."""
    install_script = Path("/root/commontrace-v2/install.sh")
    res = subprocess.run(
        ["bash", str(install_script), "--in-place", "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
    assert "Install complete" in res.stdout or "Installation complete" in res.stdout or "Setup complete" in res.stdout or "Done" in res.stdout


def test_r1_f5_install_sh_references_valid_reference_paths() -> None:
    """Validate that install.sh does not reference obsolete or broken paths for build_index."""
    install_script = Path("/root/commontrace-v2/install.sh")
    content = install_script.read_text(encoding="utf-8")
    # Verify that the script contains valid references to the reference directory or cli index
    assert "commontrace/reference/build_index.py" in content or "commontrace.cli index" in content or "commontrace index" in content or "memory/attention/build_index.py" in content
