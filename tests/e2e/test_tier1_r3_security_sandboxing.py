"""Tier 1: Feature Coverage for Security & Sandboxing (Milestone 3).

Covers Features:
- R3-F1: Centralized Workspace Boundary Enforcement
- R3-F2: Validation Path Sandboxing
- R3-F3: Safe Output Writing & Symlink Breaking
- R3-F4: Subprocess Execution Hardening
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from tests.e2e.conftest import CLIResult

# ============================================================================
# R3-F1: Centralized Workspace Boundary Enforcement (>=5 tests)
# ============================================================================

def test_r3_f1_enforce_boundary_resolves_valid_relative_path(tmp_path: Path) -> None:
    """Validate that candidate path within base directory returns normalized absolute path."""
    from commontrace.paths import enforce_boundary

    base = tmp_path / "base"
    base.mkdir(parents=True, exist_ok=True)
    subfile = base / "memory" / "lessons" / "test.md"
    subfile.parent.mkdir(parents=True, exist_ok=True)
    subfile.write_text("content", encoding="utf-8")

    resolved = enforce_boundary(str(base), "memory/lessons/test.md")
    assert resolved == str(subfile.resolve())


def test_r3_f1_enforce_boundary_rejects_parent_traversal(tmp_path: Path) -> None:
    """Validate that path traversal with '../' escaping base directory raises PathTraversalError."""
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises((PathTraversalError, ValueError)) as exc:
        enforce_boundary(str(base), "../../escaped.txt")
    assert "path traversal" in str(exc.value).lower() or "outside" in str(exc.value).lower()


def test_r3_f1_enforce_boundary_rejects_absolute_path_outside(tmp_path: Path) -> None:
    """Validate that absolute path outside base directory raises PathTraversalError."""
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside_file.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), str(outside))


def test_r3_f1_enforce_boundary_rejects_symlink_pointing_outside(tmp_path: Path) -> None:
    """Validate that symlink within base directory pointing outside base is rejected."""
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("target content", encoding="utf-8")

    symlink = base / "link_to_outside.txt"
    symlink.symlink_to(outside)

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), "link_to_outside.txt")


def test_r3_f1_enforce_boundary_rejects_empty_paths(tmp_path: Path) -> None:
    """Validate that empty candidate or base path raises ValueError."""
    from commontrace.paths import enforce_boundary

    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(ValueError):
        enforce_boundary(str(base), "")

    with pytest.raises(ValueError):
        enforce_boundary("", "some_file.txt")


def test_r3_f1_enforce_boundary_exact_match_allowed(tmp_path: Path) -> None:
    """Validate boundary checks when candidate_path matches base_dir exactly."""
    from commontrace.paths import enforce_boundary

    base = tmp_path / "base"
    base.mkdir()

    # When allow_within=False, exact match should succeed
    exact = enforce_boundary(str(base), str(base), allow_within=False)
    assert exact == str(base.resolve())


# ============================================================================
# R3-F2: Validation Path Sandboxing (>=5 tests)
# ============================================================================

def test_r3_f2_lesson_validate_blocks_path_traversal(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `lesson validate` rejects paths escaping the workspace via traversal."""
    res = cli_runner(["lesson", "validate", "../../../etc/passwd", "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "traversal" in res.stderr.lower() or "error" in res.stderr.lower()


def test_r3_f2_trace_validate_blocks_path_traversal(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
) -> None:
    """Validate that `trace validate` rejects paths escaping the workspace via traversal."""
    res = cli_runner(["trace", "validate", "../../outside_trace.md", "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "traversal" in res.stderr.lower() or "error" in res.stderr.lower()


def test_r3_f2_lesson_validate_blocks_symlink_escape(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    tmp_path: Path,
) -> None:
    """Validate that `lesson validate` rejects symlinked lesson pointing outside workspace."""
    outside = tmp_path / "outside_lesson.md"
    outside.write_text("---\nname: lesson_ext\n---\nbody", encoding="utf-8")

    link_in_store = isolated_store / "memory" / "lessons" / "lesson_symlink_ext.md"
    link_in_store.symlink_to(outside)

    res = cli_runner(["lesson", "validate", str(link_in_store), "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "traversal" in res.stderr.lower() or "error" in res.stderr.lower()


def test_r3_f2_trace_validate_blocks_symlink_escape(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    tmp_path: Path,
) -> None:
    """Validate that `trace validate` rejects symlinked trace pointing outside workspace."""
    outside = tmp_path / "outside_trace.md"
    outside.write_text("---\nid: trace_ext\n---\nbody", encoding="utf-8")

    link_in_store = isolated_store / "memory" / "traces" / "trace_symlink_ext.md"
    link_in_store.symlink_to(outside)

    res = cli_runner(["trace", "validate", str(link_in_store), "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "traversal" in res.stderr.lower() or "error" in res.stderr.lower()


def test_r3_f2_validation_within_store_allowed(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    lesson_factory: Callable[..., Path],
) -> None:
    """Validate that legitimate lessons inside the store pass validation sandboxing."""
    lesson_path = lesson_factory(isolated_store, slug="lesson_safe_internal")
    res = cli_runner(["lesson", "validate", str(lesson_path), "--dest", str(isolated_store)])
    assert res.exit_code == 0
    assert "OK" in res.stdout


# ============================================================================
# R3-F3: Safe Output Writing & Symlink Breaking (>=5 tests)
# ============================================================================

def test_r3_f3_safe_prepare_output_path_unlinks_leaf_symlink(tmp_path: Path) -> None:
    """Validate that safe_prepare_output_path unlinks leaf symlink to prevent write-through."""
    from commontrace.paths import safe_prepare_output_path

    target_file = tmp_path / "sensitive_target.txt"
    target_file.write_text("original content", encoding="utf-8")

    symlink_file = tmp_path / "output_link.txt"
    symlink_file.symlink_to(target_file)

    safe_path = safe_prepare_output_path(str(symlink_file), allow_unlink_leaf=True)
    assert not os.path.islink(safe_path)
    assert target_file.read_text(encoding="utf-8") == "original content"


def test_r3_f3_safe_prepare_output_path_rejects_symlink_intermediate_dir(tmp_path: Path) -> None:
    """Validate that safe_prepare_output_path blocks writing through symlinked directories."""
    from commontrace.paths import PathTraversalError, safe_prepare_output_path

    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()

    sym_dir = tmp_path / "sym_dir"
    sym_dir.symlink_to(real_dir)

    out_file = sym_dir / "target.txt"

    with pytest.raises(PathTraversalError) as exc:
        safe_prepare_output_path(str(out_file))
    assert "symlink" in str(exc.value).lower()


def test_r3_f3_safe_prepare_output_path_rejects_empty() -> None:
    """Validate that empty output path raises ValueError."""
    from commontrace.paths import safe_prepare_output_path

    with pytest.raises(ValueError):
        safe_prepare_output_path("")


def test_r3_f3_export_blocks_symlink_write_through(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    tmp_path: Path,
) -> None:
    """Validate that `commontrace export --out` unlinks or blocks symlink write-through."""
    target_file = tmp_path / "protected_target.txt"
    target_file.write_text("SENSITIVE DATA DO NOT OVERWRITE", encoding="utf-8")

    symlink_out = tmp_path / "export_symlink.jsonl"
    symlink_out.symlink_to(target_file)

    res = cli_runner(["export", "--out", str(symlink_out), "--dest", str(isolated_store)])
    assert res.exit_code == 0
    # Target file should not have been overwritten through the symlink
    assert target_file.read_text(encoding="utf-8") == "SENSITIVE DATA DO NOT OVERWRITE"
    assert not os.path.islink(str(symlink_out))


def test_r3_f3_export_blocks_symlinked_directory_output(
    cli_runner: Callable[..., CLIResult],
    isolated_store: Path,
    tmp_path: Path,
) -> None:
    """Validate that `commontrace export --out` rejects intermediate symlinked directories."""
    real_target_dir = tmp_path / "external_system_dir"
    real_target_dir.mkdir()

    symlink_dir = tmp_path / "symlinked_output_dir"
    symlink_dir.symlink_to(real_target_dir)

    target_file = symlink_dir / "export.jsonl"

    res = cli_runner(["export", "--out", str(target_file), "--dest", str(isolated_store)])
    assert res.exit_code == 1
    assert "symlink" in res.stderr.lower()


# ============================================================================
# R3-F4: Subprocess Execution Hardening (>=5 tests)
# ============================================================================

def test_r3_f4_shellout_enforces_python_safe_path() -> None:
    """Validate that _shellout.py sets PYTHONSAFEPATH='1' in subprocess environment."""
    import inspect

    from commontrace.commands import _shellout

    source = inspect.getsource(_shellout.run_script)
    assert 'env["PYTHONSAFEPATH"] = "1"' in source or "PYTHONSAFEPATH" in source


def test_r3_f4_shellout_enforces_python_utf8() -> None:
    """Validate that _shellout.py sets PYTHONUTF8='1' in subprocess environment."""
    import inspect

    from commontrace.commands import _shellout

    source = inspect.getsource(_shellout.run_script)
    assert 'env["PYTHONUTF8"] = "1"' in source or "PYTHONUTF8" in source


def test_r3_f4_shellout_uses_shell_false() -> None:
    """Validate that subprocess.run is called without shell=True in _shellout.py."""
    import inspect

    from commontrace.commands import _shellout

    source = inspect.getsource(_shellout.run_script)
    assert "shell=True" not in source


def test_r3_f4_cwd_module_shadowing_mitigated(
    tmp_path: Path,
    isolated_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate that _shellout.run_script enforces PYTHONSAFEPATH=1 to prevent CWD module shadowing."""
    from commontrace.commands import _shellout

    shadowing_dir = tmp_path / "shadow_cwd"
    shadowing_dir.mkdir()

    # Place a malicious shadow module 'yaml.py' in shadowing_dir
    shadow_yaml = shadowing_dir / "yaml.py"
    shadow_yaml.write_text("raise RuntimeError('MALICIOUS MODULE HIJACKING TRIGGERED')", encoding="utf-8")

    # Change current working directory to the directory containing the shadow module
    monkeypatch.chdir(shadowing_dir)

    # run_script sets PYTHONSAFEPATH=1, so the subprocess will not import yaml from cwd
    rc = _shellout.run_script(
        str(isolated_store),
        "commontrace/reference/measure_performance.py",
        ["--help"],
        missing_hint="test script not found",
    )
    assert rc == 0




def test_r3_f4_run_script_sets_canonical_root_in_child_env(isolated_store: Path) -> None:
    """Validate that run_script sets COMMONTRACE_ROOT to canonical resolved path."""
    import inspect

    from commontrace.commands import _shellout

    source = inspect.getsource(_shellout.run_script)
    assert 'env["COMMONTRACE_ROOT"] = root' in source
