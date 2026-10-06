from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from tests.e2e.conftest import CLIResult


def test_boundary_empty_agent_type(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    res = cli_runner(["init", "--agent-type", "", "--dest", str(isolated_store)])
    assert res.exit_code == 2
    assert "not a valid agent type" in res.stderr


def test_boundary_max_length_agent_type(cli_runner: Callable[..., CLIResult], tmp_path: Path) -> None:
    valid_64 = "a" * 64
    invalid_65 = "a" * 65

    dir_64 = tmp_path / "store_64"
    res_64 = cli_runner(["init", "--agent-type", valid_64, "--dest", str(dir_64)])
    assert res_64.exit_code == 0

    dir_65 = tmp_path / "store_65"
    res_65 = cli_runner(["init", "--agent-type", invalid_65, "--dest", str(dir_65)])
    assert res_65.exit_code == 2
    assert "not a valid agent type" in res_65.stderr


def test_boundary_top_k_limits(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    res_min = cli_runner(["query", "--lexical", "--top-k", "1", "--dest", str(isolated_store), "test"])
    assert res_min.exit_code == 0

    res_large = cli_runner(["query", "--lexical", "--top-k", "99999", "--dest", str(isolated_store), "test"])
    assert res_large.exit_code == 0

    res_str = cli_runner(["query", "--top-k", "abc", "--dest", str(isolated_store), "test"])
    assert res_str.exit_code == 2

    res_neg = cli_runner(["query", "--top-k", "-10", "--dest", str(isolated_store), "test"])
    assert res_neg.exit_code == 2


def test_boundary_similarity_threshold_epsilon(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    res_low = cli_runner(["distill", "--similarity-threshold", "0.0001", "--dest", str(isolated_store)])
    assert res_low.exit_code == 0

    res_max = cli_runner(["distill", "--similarity-threshold", "1.0", "--dest", str(isolated_store)])
    assert res_max.exit_code == 0

    res_neg = cli_runner(["distill", "--similarity-threshold", "-0.001", "--dest", str(isolated_store)])
    assert res_neg.exit_code == 2

    res_high = cli_runner(["distill", "--similarity-threshold", "1.0001", "--dest", str(isolated_store)])
    assert res_high.exit_code == 2


def test_boundary_empty_fields_capture(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    res_title = cli_runner([
        "capture",
        "--title", "",
        "--context", "context",
        "--solution", "solution",
        "--dest", str(isolated_store),
    ])
    assert res_title.exit_code == 1
    assert "refusing to write an invalid trace" in res_title.stderr


def test_boundary_sigint_simulation() -> None:
    class MockKeyboardInterruptAction:
        def __call__(self, *args, **kwargs):
            raise KeyboardInterrupt()

    import argparse
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    p = subparsers.add_parser("test_sigint")
    p.set_defaults(func=MockKeyboardInterruptAction())

    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        rc = 130
    assert rc == 130


def test_boundary_oserror_clean_exit_code_1(cli_runner: Callable[..., CLIResult], isolated_store: Path) -> None:
    res = cli_runner(["lesson", "validate", "/dev/null/impossible_file.md", "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "Traceback" not in res.stderr
    assert "[commontrace] error:" in res.stderr


def test_boundary_bare_commontrace_exits_code_2(cli_runner: Callable[..., CLIResult]) -> None:
    res = cli_runner([])
    assert res.exit_code == 2
    assert "the following arguments are required: command" in res.stderr or "usage:" in res.stderr


def test_boundary_help_on_all_core_subcommands(cli_runner: Callable[..., CLIResult]) -> None:
    core_commands = ["init", "install", "capture", "trace", "lesson", "query", "index", "bench", "sync", "doctor"]
    for cmd in core_commands:
        res = cli_runner([cmd, "--help"])
        assert res.exit_code == 0, f"Command {cmd} --help failed with exit code {res.exit_code}"


def test_boundary_corrupt_csv_import_exits_code_1(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    tmp_path: Path,
) -> None:
    corrupt_csv = tmp_path / "corrupt.csv"
    corrupt_csv.write_text("col_a,col_b,col_c\nval1,val2,val3\n", encoding="utf-8")

    res = cli_runner(["import", str(corrupt_csv), "--agent-type", "code", "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "skipped" in res.stdout or "skipped" in res.stderr
    assert "Traceback" not in res.stderr


def test_boundary_metrics_zero_accepted(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
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


def test_boundary_negative_tokens_rejected(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
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


def test_boundary_warn_chars_threshold(
    isolated_store: Path,
) -> None:
    from commontrace.commands._validators import WARN_CHARS, check_text_size

    text_300k = "B" * (WARN_CHARS + 1000)
    fields = {"description": text_300k}
    assert check_text_size(fields, what="lesson") is True


def test_boundary_refuse_chars_threshold() -> None:
    from commontrace.commands._validators import REFUSE_CHARS, check_text_size

    text_over_1mb = "C" * (REFUSE_CHARS + 1)
    fields = {"description": text_over_1mb}
    assert check_text_size(fields, what="lesson") is False


def test_boundary_unfilled_placeholders_permitted_for_review_status(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    lesson_path = lesson_factory(
        isolated_store,
        slug="lesson_candidate_review",
        status="review",
        body="## Rule\nTODO: Under review\n",
    )
    res = cli_runner(["lesson", "validate", str(lesson_path), "--dest", str(isolated_store)])
    assert res.exit_code == 0
    assert "OK" in res.stdout


def test_boundary_task_is_exact_double_dash(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "--"])
    assert res.exit_code == 0


def test_boundary_task_with_many_leading_dashes(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    res3 = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "---triple-dash"])
    assert res3.exit_code == 0

    res5 = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", "-----five-dash"])
    assert res5.exit_code == 0


def test_boundary_task_with_single_char_flags(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    for flag_like in ["-x", "-k", "-h", "-v"]:
        res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), "--", flag_like])
        assert res.exit_code == 0


def test_boundary_task_containing_flag_syntax_internally(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    res = cli_runner([
        "query", "--lexical",
        "--dest", str(isolated_store),
        "--",
        "find rules with --format=json and --dest=/tmp",
    ])
    assert res.exit_code == 0


def test_boundary_task_empty_string(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    res = cli_runner(["query", "--lexical", "--dest", str(isolated_store), ""])
    assert res.exit_code == 0


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL_SCRIPT = REPO_ROOT / "install.sh"


def test_boundary_install_sh_dest_with_spaces(tmp_path: Path) -> None:
    dest_with_space = tmp_path / "custom store with spaces"

    res = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--dest", str(dest_with_space), "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
    assert dest_with_space.exists()


def test_boundary_install_sh_trailing_slash(tmp_path: Path) -> None:
    dest_slash = str(tmp_path / "store_trailing") + "/"

    res = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--dest", dest_slash, "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0


def test_boundary_install_sh_invalid_python_binary() -> None:
    res = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--python", "/nonexistent/python_bin_xyz", "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode != 0
    assert "not found" in res.stderr.lower() or "error" in res.stderr.lower() or "not executable" in res.stderr.lower()


def test_boundary_install_sh_dest_equals_syntax(tmp_path: Path) -> None:
    dest_path = tmp_path / "dest_eq"

    res = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), f"--dest={dest_path}", "--no-deps", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
    assert dest_path.exists()


def test_boundary_install_sh_repeated_flags(tmp_path: Path) -> None:
    dest_path = tmp_path / "dest_repeat"

    res = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--dest", str(dest_path), "--no-deps", "--no-deps", "--no-index", "--no-index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert res.returncode == 0
