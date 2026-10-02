from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from tests.e2e.conftest import CLIResult


def test_r3_f1_boundary_dot_and_current_directory(tmp_path: Path) -> None:
    from commontrace.paths import enforce_boundary

    base = tmp_path / "sandbox"
    base.mkdir(parents=True, exist_ok=True)

    resolved = enforce_boundary(str(base), ".")
    assert resolved == str(base.resolve())


def test_r3_f1_boundary_redundant_inner_navigation(tmp_path: Path) -> None:
    from commontrace.paths import enforce_boundary

    base = tmp_path / "sandbox"
    subdir = base / "memory" / "lessons"
    subdir.mkdir(parents=True, exist_ok=True)
    target = subdir / "lesson_a.md"
    target.write_text("dummy", encoding="utf-8")

    complex_path = "memory/lessons/../../memory/lessons/lesson_a.md"
    resolved = enforce_boundary(str(base), complex_path)
    assert resolved == str(target.resolve())


def test_r3_f1_boundary_symlink_inside_pointing_outside(tmp_path: Path) -> None:
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
    from commontrace.paths import PathTraversalError, enforce_boundary

    base = tmp_path / "sandbox"
    base.mkdir()

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), "/")

    with pytest.raises((PathTraversalError, ValueError)):
        enforce_boundary(str(base), "/etc/passwd")


def test_r3_f1_boundary_nonexistent_nested_target(tmp_path: Path) -> None:
    from commontrace.paths import enforce_boundary

    base = tmp_path / "sandbox"
    base.mkdir()

    candidate = "memory/traces/new_trace_01.md"
    resolved = enforce_boundary(str(base), candidate)
    expected = str((base / "memory" / "traces" / "new_trace_01.md").resolve())
    assert resolved == expected


def test_r3_f2_boundary_validate_symlink_to_outside_rejected(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
    tmp_path: Path,
) -> None:
    outside_file = tmp_path / "outside_lesson.md"
    outside_file.write_text("---\nname: Outside\n---\n", encoding="utf-8")

    symlink_lesson = isolated_store / "memory" / "lessons" / "lesson_symlink_outside.md"
    try:
        symlink_lesson.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlink creation not supported")

    res = cli_runner(["lesson", "validate", "lesson_symlink_outside.md"], cwd=isolated_store)
    assert res.exit_code != 0
    assert "error" in res.stderr.lower() or "traversal" in res.stderr.lower() or "invalid" in res.stderr.lower() or "outside" in res.stderr.lower()


def test_r3_f2_boundary_validate_directory_instead_of_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    res = cli_runner(["lesson", "validate", "memory/lessons"], cwd=isolated_store)
    assert res.exit_code != 0
    assert "Traceback" not in res.stderr


def test_r3_f2_boundary_validate_zero_byte_empty_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    empty_file = isolated_store / "memory" / "lessons" / "lesson_empty.md"
    empty_file.write_text("", encoding="utf-8")

    res = cli_runner(["lesson", "validate", str(empty_file)], cwd=isolated_store)
    assert res.exit_code != 0
    assert "Traceback" not in res.stderr


def test_r3_f2_boundary_validate_nonexistent_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
    res = cli_runner(["lesson", "validate", "lesson_does_not_exist_xyz.md"], cwd=isolated_store)
    assert res.exit_code == 1
    assert "Traceback" not in res.stderr
    assert "no such file" in res.stderr.lower() or "error" in res.stderr.lower()


def test_r3_f2_boundary_validate_permission_denied_file(
    isolated_store: Path,
    cli_runner: Callable[..., CLIResult],
) -> None:
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


def test_r3_f3_boundary_safe_prepare_dangling_symlink_broken(tmp_path: Path) -> None:
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
    assert not symlink_file.exists()

    safe_path = safe_prepare_output_path(str(symlink_file), allow_unlink_leaf=True)
    assert not Path(safe_path).is_symlink()


def test_r3_f3_boundary_safe_prepare_symlink_to_target_unlinked(tmp_path: Path) -> None:
    from commontrace.paths import safe_prepare_output_path

    decoy = tmp_path / "important_target.txt"
    decoy.write_text("PRESERVE_ME", encoding="utf-8")

    symlink_dest = tmp_path / "link_dest.txt"
    try:
        symlink_dest.symlink_to(decoy)
    except OSError:
        pytest.skip("Symlink creation not supported")

    safe_path = safe_prepare_output_path(str(symlink_dest), allow_unlink_leaf=True)
    assert decoy.read_text(encoding="utf-8") == "PRESERVE_ME"
    assert not Path(safe_path).is_symlink()


def test_r3_f3_boundary_safe_prepare_allow_unlink_leaf_false_rejects(tmp_path: Path) -> None:
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
    lesson_factory(isolated_store, slug="lesson_export_l1", title="Export 1")

    target_file = tmp_path / "target_decoy.jsonl"
    target_file.write_text("KEEP_ORIGINAL_DATA", encoding="utf-8")

    export_dest = isolated_store / "export_symlink.jsonl"
    try:
        export_dest.symlink_to(target_file)
    except OSError:
        pytest.skip("Symlink creation not supported")

    res = cli_runner(["trace", "export", "--dest", str(export_dest), "--format", "jsonl"], cwd=isolated_store)
    assert target_file.read_text(encoding="utf-8") == "KEEP_ORIGINAL_DATA"


def test_r3_f4_boundary_shell_metacharacters_in_arguments(tmp_path: Path) -> None:
    from commontrace.commands import _shellout

    marker_file = tmp_path / "hacked_metachar_marker.txt"
    injection_arg = f"; touch {marker_file} ;"

    code, out = _shellout.run_script(
        root=str(tmp_path),
        relative="reference/bench.py",
        extra_args=[injection_arg],
        missing_hint="missing",
        capture=True,
    )
    assert not marker_file.exists()


def test_r3_f4_boundary_command_substitution_not_expanded(tmp_path: Path) -> None:
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
    from commontrace.commands import _shellout

    pipe_arg = "foo | cat | grep something"
    code, out = _shellout.run_script(
        root=str(tmp_path),
        relative="reference/bench.py",
        extra_args=[pipe_arg],
        missing_hint="missing",
        capture=True,
    )
    assert isinstance(code, int)


def test_r3_f4_boundary_env_preserves_pythonsafepath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
