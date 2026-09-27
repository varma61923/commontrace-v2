"""Tests for workspace boundary sandboxing, path traversal defenses, and symlink security."""
import os

import pytest

from commontrace.cli import main
from commontrace.paths import (
    PathTraversalError,
    enforce_boundary,
    is_within_directory,
    safe_prepare_output_path,
)

# ============================================================================
# 1. enforce_boundary & is_within_directory Unit Tests
# ============================================================================

class TestEnforceBoundary:
    def test_enforce_boundary_valid_contained_file(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()
        subfile = base / "file.txt"
        subfile.write_text("content", encoding="utf-8")

        result = enforce_boundary(str(base), str(subfile))
        assert result == os.path.realpath(str(subfile))

    def test_enforce_boundary_relative_path_within(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()
        subdir = base / "sub"
        subdir.mkdir()
        (subdir / "target.md").write_text("content", encoding="utf-8")

        result = enforce_boundary(str(base), "sub/target.md")
        assert result == os.path.realpath(str(subdir / "target.md"))

    def test_enforce_boundary_rejects_empty_inputs(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(ValueError, match="base_dir must not be empty"):
            enforce_boundary("", "file.txt")

        with pytest.raises(ValueError, match="candidate_path must not be empty"):
            enforce_boundary(base, "")

    def test_enforce_boundary_rejects_parent_traversal(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("secret", encoding="utf-8")

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            enforce_boundary(str(base), "../secret.txt")

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            enforce_boundary(str(base), "sub/../../secret.txt")

    def test_enforce_boundary_rejects_absolute_path_outside(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            enforce_boundary(str(base), "/etc/passwd")

    def test_enforce_boundary_rejects_symlink_pointing_outside(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("outside", encoding="utf-8")

        symlink_inside = base / "leak_link"
        os.symlink(str(outside_file), str(symlink_inside))

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            enforce_boundary(str(base), str(symlink_inside))

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            enforce_boundary(str(base), "leak_link")

    def test_enforce_boundary_rejects_symlinked_directory_pointing_outside(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        outside_file = outside_dir / "target.txt"
        outside_file.write_text("outside target", encoding="utf-8")

        symlink_dir = base / "sym_dir"
        os.symlink(str(outside_dir), str(symlink_dir))

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            enforce_boundary(str(base), str(symlink_dir / "target.txt"))

    def test_enforce_boundary_allow_within_false(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()
        subfile = base / "file.txt"
        subfile.write_text("content", encoding="utf-8")

        # Equal to base should succeed
        assert enforce_boundary(str(base), str(base), allow_within=False) == os.path.realpath(str(base))

        # Subfile within base should fail when allow_within=False
        with pytest.raises(PathTraversalError, match="path traversal detected"):
            enforce_boundary(str(base), str(subfile), allow_within=False)

    def test_is_within_directory(self, tmp_path):
        base = tmp_path / "workspace"
        base.mkdir()
        subfile = base / "file.txt"
        subfile.write_text("ok", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("no", encoding="utf-8")

        assert is_within_directory(str(base), str(subfile)) is True
        assert is_within_directory(str(base), "file.txt") is True
        assert is_within_directory(str(base), str(base)) is True
        assert is_within_directory(str(base), str(outside)) is False
        assert is_within_directory(str(base), "../outside.txt") is False


# ============================================================================
# 2. safe_prepare_output_path Symlink Defense Tests
# ============================================================================

class TestSafePrepareOutputPath:
    def test_empty_output_path_raises(self):
        with pytest.raises(ValueError, match="Output path must not be empty"):
            safe_prepare_output_path("")

    def test_normal_output_path_creates_parents(self, tmp_path):
        target = tmp_path / "nested" / "deep" / "output.jsonl"
        assert not target.parent.exists()

        prepared = safe_prepare_output_path(str(target))
        assert prepared == os.path.abspath(str(target))
        assert target.parent.exists()
        assert not target.exists()

    def test_rejects_intermediate_symlink_directory(self, tmp_path):
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sym_dir = workspace / "symlink_to_outside"
        os.symlink(str(outside_dir), str(sym_dir))

        target = sym_dir / "exploit.jsonl"
        with pytest.raises(PathTraversalError, match="refusing to write through symlinked directory"):
            safe_prepare_output_path(str(target))

    def test_unlinks_leaf_symlink_when_allowed(self, tmp_path):
        victim = tmp_path / "victim_secret.txt"
        victim.write_text("CRITICAL_SECRET_CONTENT", encoding="utf-8")

        out_link = tmp_path / "export_target.jsonl"
        os.symlink(str(victim), str(out_link))

        # With allow_unlink_leaf=True (default)
        prepared = safe_prepare_output_path(str(out_link), allow_unlink_leaf=True)
        assert prepared == os.path.abspath(str(out_link))

        # The symlink itself must have been unlinked
        assert not os.path.islink(str(out_link))

        # The victim file must NOT have been overwritten
        assert victim.read_text(encoding="utf-8") == "CRITICAL_SECRET_CONTENT"

        # Now writing to the prepared path writes to a clean new file
        with open(prepared, "w", encoding="utf-8") as fh:
            fh.write("NEW_EXPORT_DATA")

        assert out_link.read_text(encoding="utf-8") == "NEW_EXPORT_DATA"
        assert victim.read_text(encoding="utf-8") == "CRITICAL_SECRET_CONTENT"

    def test_rejects_leaf_symlink_when_allow_unlink_leaf_false(self, tmp_path):
        victim = tmp_path / "victim_secret.txt"
        victim.write_text("CRITICAL_SECRET_CONTENT", encoding="utf-8")

        out_link = tmp_path / "export_target.jsonl"
        os.symlink(str(victim), str(out_link))

        with pytest.raises(PathTraversalError, match="refusing to write through leaf symlink"):
            safe_prepare_output_path(str(out_link), allow_unlink_leaf=False)

        assert os.path.islink(str(out_link))
        assert victim.read_text(encoding="utf-8") == "CRITICAL_SECRET_CONTENT"


# ============================================================================
# 3. CLI Validation Path Sandboxing Tests
# ============================================================================

class TestCliValidationPathSandboxing:
    def test_lesson_validate_rejects_path_outside_workspace(self, tmp_path, capsys):
        outside_file = tmp_path / "outside_lesson.md"
        outside_file.write_text(
            "---\nname: lesson_test\n---\n## Rule\nTest rule\n",
            encoding="utf-8",
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Run within workspace
        exit_code = main(["lesson", "validate", "--dest", str(workspace), str(outside_file)])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "PathTraversalError" in captured.err
        assert "resolves outside boundary" in captured.err

    def test_trace_validate_rejects_path_outside_workspace(self, tmp_path, capsys):
        outside_trace = tmp_path / "outside_trace.json"
        outside_trace.write_text('{"id": "test"}', encoding="utf-8")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        exit_code = main(["trace", "validate", "--dest", str(workspace), str(outside_trace)])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "PathTraversalError" in captured.err
        assert "resolves outside boundary" in captured.err

    def test_lesson_validate_rejects_etc_passwd(self, capsys):
        exit_code = main(["lesson", "validate", "/etc/passwd"])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "PathTraversalError" in captured.err
        assert "cannot read: path traversal detected: '/etc/passwd'" in captured.err

    def test_trace_validate_rejects_etc_passwd(self, capsys):
        exit_code = main(["trace", "validate", "/etc/passwd"])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "PathTraversalError" in captured.err
        assert "cannot read: path traversal detected: '/etc/passwd'" in captured.err

    def test_export_cmd_symlink_defense(self, tmp_path):
        victim = tmp_path / "victim.txt"
        victim.write_text("TARGET_SECRET", encoding="utf-8")

        workspace = tmp_path / "ws"
        (workspace / "memory" / "lessons").mkdir(parents=True)
        out_symlink = tmp_path / "export.jsonl"
        os.symlink(str(victim), str(out_symlink))

        exit_code = main(["export", "--dest", str(workspace), "--out", str(out_symlink)])
        assert exit_code == 0
        assert victim.read_text(encoding="utf-8") == "TARGET_SECRET"
        assert not os.path.islink(str(out_symlink))
