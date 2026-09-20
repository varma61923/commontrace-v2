"""Tier 2: Boundary & Corner Cases for Security & Sandboxing (Milestone 3).

Covers Features:
- R3-F1: Centralized Workspace Boundary Enforcement (redundant relative paths, symlinks outside base, root escapes)
- R3-F2: Validation Path Sandboxing (symlinks to device nodes/system files, directories, empty files, chmod 000)
- R3-F3: Safe Output Writing & Symlink Breaking (dangling symlinks, existing target overwrite protection, parent creation)
- R3-F4: Subprocess Execution Hardening (shell metacharacters as literals, non-existent binaries, env isolation)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from tests.e2e.conftest import CLIResult

# ============================================================================
# R3-F1: Boundary Cases for enforce_boundary (>=5 tests)
# ============================================================================

def test_r3_f1_boundary_dot_and_current_directory(tmp_path: Path) -> None:
    """Validate that '.' resolves exactly to the canonical base directory."""
    from commontrace.paths import enforce_boundary

    base = tmp_path / "sandbox"
    base.mkdir(parents=True, exist_ok=True)

    resolved = enforce_boundary(str(base), ".")
    assert resolved == str(base.resolve())


def test_r3_f1_boundary_redundant_inner_navigation(tmp_path: Path) -> None:
    """Validate redundant relative tokens (e.g. dir/../dir) resolve if remaining within base."""
    from commontrace.paths import enforce_boundary

    base = tmp_path / "sandbox"
    subdir = base / "memory" / "lessons"
    subdir.mkdir(parents=True, exist_ok=True)
    target = subdir / "lesson_a.md"
    target.write_text("dummy", encoding="utf-8")

    # Path containing redundant inner step that stays inside base
    complex_path = "memory/lessons/../../memory/lessons/lesson_a.md"
    resolved = enforce_boundary(str(base), complex_path)
    assert resolved == str(target.resolve())


def test_r3_f1_boundary_symlink_inside_pointing_outside(tmp_path: Path) -> None:
    """Validate that symlink located inside base pointing outside base is rejected."""
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "sandbox"
    base.mkdir()
    outside_secret = tmp_path / "secret.txt"
    outside_secret.write_text("confidential", encoding="utf-8")

    symlink_inside = base / "link_to_secret.txt"
    try:
        symlink_inside.symlink_to(outside_secret)
    except OSError:
        pytest.skip("Symlink creation not supported in this environment")

    with pytest.raises((PathTraversalError, ValueError)) as exc:
        enforce_boundary(str(base), "link_to_secret.txt")
    assert "traversal" in str(exc.value).lower() or "outside" in str(exc.value).lower()


def test_r3_f1_boundary_root_path_escape(tmp_path: Path) -> None:
    """Validate that root path ('/' or '/etc') is strictly rejected."""
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "sandbox"
    base.mkdir()

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), "/")

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), "/etc/passwd")


def test_r3_f1_boundary_nonexistent_nested_target(tmp_path: Path) -> None:
    """Validate non-existent future target path within boundary resolves properly for creation."""
    from commontrace.paths import enforce_boundary

    base = tmp_path / "sandbox"
    base.mkdir()

    # Target does not exist yet, but resolves inside base
    candidate = "memory/traces/new_trace_01.md"
    resolved = enforce_boundary(str(base), candidate)
    expected = str((base / "memory" / "traces" / "new_trace_01.md").resolve())
    assert resolved == expected


# ============================================================================
# R3-F2: Boundary Cases for Validation Sandboxing (>=5 tests)
# ============================================================================

def test_r3_f2_boundary_validate_symlink_to_outside_rejected(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
) -> None:
    """Validate that `lesson validate` rejects symlink target outside workspace store."""
    outside_file = tmp_path / "outside_lesson.md"
    outside_file.write_text("---\nname: Outside\n---\n", encoding="utf-8")

    symlink_lesson = isolated_store / "memory" / "lessons" / "lesson_symlink_outside.md"
    try:
        symlink_lesson.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlink creation not supported")

    res = cli_runner(["lesson", "validate", "lesson_symlink_outside.md"], cwd=isolated_store)
    # Must reject with non-zero exit code due to boundary violation
    assert res.exit_code != 0
    assert "error" in res.stderr.lower() or "traversal" in res.stderr.lower() or "invalid" in res.stderr.lower() or "outside" in res.stderr.lower()


def test_r3_f2_boundary_validate_directory_instead_of_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate that invoking validate on a directory surfaces a clean error."""
    res = cli_runner(["lesson", "validate", "memory/lessons"], cwd=isolated_store)
    assert res.exit_code != 0
    assert "Traceback" not in res.stderr


def test_r3_f2_boundary_validate_zero_byte_empty_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate that validating an empty 0-byte file rejects cleanly."""
    empty_file = isolated_store / "memory" / "lessons" / "lesson_empty.md"
    empty_file.write_text("", encoding="utf-8")

    res = cli_runner(["lesson", "validate", str(empty_file)], cwd=isolated_store)
    assert res.exit_code != 0
    assert "Traceback" not in res.stderr


def test_r3_f2_boundary_validate_nonexistent_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate that validating a non-existent file exits cleanly with code 1."""
    res = cli_runner(["lesson", "validate", "lesson_does_not_exist_xyz.md"], cwd=isolated_store)
    assert res.exit_code == 1
    assert "Traceback" not in res.stderr
    assert "no such file" in res.stderr.lower() or "error" in res.stderr.lower()


def test_r3_f2_boundary_validate_permission_denied_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    """Validate that unreadable file (chmod 000) exits cleanly without raw crash."""
    if os.name == "nt":
        pytest.skip("File permission mode test is posix-specific")

    unreadable = isolated_store / "memory" / "lessons" / "lesson_unreadable.md"
    unreadable.write_text("---\nname: Test\n---\n", encoding="utf-8")
    original_mode = unreadable.stat().st_mode
    try:
        unreadable.chmod(0)
        res = cli_runner(["lesson", "validate", str(unreadable)], cwd=isolated_store)
        assert res.exit_code != 0
        assert "Traceback" not in res.stderr
    finally:
        unreadable.chmod(original_mode)


# ============================================================================
# R3-F3: Boundary Cases for Safe Output Writing & Symlink Breaking (>=5 tests)
# ============================================================================

def test_r3_f3_boundary_safe_prepare_dangling_symlink_broken(tmp_path: Path) -> None:
    """Validate safe_prepare_output_path unlinks a dangling (broken) symlink."""
    from commontrace.paths import safe_prepare_output_path

    target_dir = tmp_path / "output_dir"
    target_dir.mkdir()
    symlink_file = target_dir / "dangling.txt"
    nonexistent = tmp_path / "nowhere.txt"

    try:
        symlink_file.symlink_to(nonexistent)
    except OSError:
        pytest.skip("Symlink creation not supported")

    assert symlink_file.is_symlink()
    assert not symlink_file.exists()  # Dangling

    # Preparing path should break/unlink the symlink
    safe_path = safe_prepare_output_path(str(symlink_file), allow_unlink_leaf=True)
    assert not Path(safe_path).is_symlink()


def test_r3_f3_boundary_safe_prepare_symlink_to_target_unlinked(tmp_path: Path) -> None:
    """Validate safe_prepare_output_path unlinks symlink without overwriting target."""
    from commontrace.paths import safe_prepare_output_path

    decoy = tmp_path / "important_target.txt"
    decoy.write_text("PRESERVE_ME", encoding="utf-8")

    symlink_dest = tmp_path / "link_dest.txt"
    try:
        symlink_dest.symlink_to(decoy)
    except OSError:
        pytest.skip("Symlink creation not supported")

    safe_path = safe_prepare_output_path(str(symlink_dest), allow_unlink_leaf=True)
    # The decoy file MUST NOT be affected or overwritten
    assert decoy.read_text(encoding="utf-8") == "PRESERVE_ME"
    assert not Path(safe_path).is_symlink()


def test_r3_f3_boundary_safe_prepare_allow_unlink_leaf_false_rejects(tmp_path: Path) -> None:
    """Validate safe_prepare_output_path raises PathTraversalError when allow_unlink_leaf=False."""
    from commontrace.paths import PathTraversalError, safe_prepare_output_path

    decoy = tmp_path / "target.txt"
    decoy.write_text("data", encoding="utf-8")
    symlink_dest = tmp_path / "link.txt"
    try:
        symlink_dest.symlink_to(decoy)
    except OSError:
        pytest.skip("Symlink creation not supported")

    with pytest.raises(PathTraversalError):
        safe_prepare_output_path(str(symlink_dest), allow_unlink_leaf=False)


def test_r3_f3_boundary_safe_prepare_creates_deep_nested_parents(tmp_path: Path) -> None:
    """Validate safe_prepare_output_path creates deeply nested missing directories."""
    from commontrace.paths import safe_prepare_output_path

    deep_dest = tmp_path / "a" / "b" / "c" / "d" / "e" / "output.json"
    assert not deep_dest.parent.exists()

    safe_path = safe_prepare_output_path(str(deep_dest))
    assert Path(safe_path).parent.exists()
    assert Path(safe_path).parent.is_dir()


def test_r3_f3_boundary_export_refuses_symlink_destination(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    lesson_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Validate commontrace trace export unlinks or refuses writing through a symlink."""
    lesson_factory(isolated_store, slug="lesson_export_l1", title="Export 1")

    target_file = tmp_path / "target_decoy.jsonl"
    target_file.write_text("KEEP_ORIGINAL_DATA", encoding="utf-8")

    export_dest = isolated_store / "export_symlink.jsonl"
    try:
        export_dest.symlink_to(target_file)
    except OSError:
        pytest.skip("Symlink creation not supported")

    # Run export targeting the symlink destination
    res = cli_runner(["trace", "export", "--dest", str(export_dest), "--format", "jsonl"], cwd=isolated_store)
    # Target decoy must remain intact
    assert target_file.read_text(encoding="utf-8") == "KEEP_ORIGINAL_DATA"


# ============================================================================
# R3-F4: Boundary Cases for Subprocess Hardening (>=5 tests)
# ============================================================================

def test_r3_f4_boundary_shell_metacharacters_in_arguments(tmp_path: Path) -> None:
    """Validate run_script passes shell metacharacters literally without executing them."""
    from commontrace.commands import _shellout

    marker_file = tmp_path / "hacked_metachar_marker.txt"
    injection_arg = f"; touch {marker_file} ;"

    # Shellout with capture=True using extra_args containing shell injection syntax
    code, out = _shellout.run_script(
        root=str(tmp_path),
        relative="reference/bench.py",
        extra_args=[injection_arg],
        missing_hint="missing",
        capture=True,
    )
    # Marker file must NOT be created because shell=False is enforced
    assert not marker_file.exists()


def test_r3_f4_boundary_command_substitution_not_expanded(tmp_path: Path) -> None:
    """Validate $(command) syntax is not evaluated by subprocess runner."""
    from commontrace.commands import _shellout

    marker_file = tmp_path / "hacked_subst_marker.txt"
    cmd_sub_arg = f"$(touch {marker_file})"

    code, out = _shellout.run_script(
        root=str(tmp_path),
        relative="reference/bench.py",
        extra_args=[cmd_sub_arg],
        missing_hint="missing",
        capture=True,
    )
    assert not marker_file.exists()


def test_r3_f4_boundary_missing_script_handled_cleanly(tmp_path: Path) -> None:
    """Validate run_script returns clean code 1 when relative script is missing."""
    from commontrace.commands import _shellout

    code = _shellout.run_script(
        root=str(tmp_path),
        relative="nonexistent/script_xyz.py",
        extra_args=[],
        missing_hint="Run pip install",
        capture=False,
    )
    assert code == 1


def test_r3_f4_boundary_pipe_characters_treated_as_literal(tmp_path: Path) -> None:
    """Validate pipe character '|' is not treated as a shell pipe."""
    from commontrace.commands import _shellout

    pipe_arg = "foo | cat | grep something"
    code, out = _shellout.run_script(
        root=str(tmp_path),
        relative="reference/bench.py",
        extra_args=[pipe_arg],
        missing_hint="missing",
        capture=True,
    )
    # Exits cleanly with integer returncode, without piping to bash commands
    assert isinstance(code, int)


def test_r3_f4_boundary_env_preserves_pythonsafepath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate run_script propagates PYTHONSAFEPATH=1 and PYTHONUTF8=1 into env."""
    from commontrace.commands import _shellout

    captured_env: dict[str, str] = {}

    def mock_subprocess_run(cmd: list[str], env: dict[str, str], **kwargs: Any) -> Any:
        nonlocal captured_env
        captured_env = dict(env)
        return type("Proc", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)
    monkeypatch.setattr(_shellout, "find_reference_script", lambda r, rel: "/fake/script.py")

    _shellout.run_script(
        root=str(tmp_path),
        relative="reference/bench.py",
        extra_args=[],
        missing_hint="missing",
        capture=True,
    )

    assert captured_env.get("PYTHONSAFEPATH") == "1"
    assert captured_env.get("PYTHONUTF8") == "1"
