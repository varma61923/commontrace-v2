"""Regression tests for Storage & Validation Remediations across M2:
- SEC-04: Atomic file writes with error cleanup in frontmatter.py:write
- SEC-05: Validation loop resilience across corrupted files in lesson_cmd.py & trace_cmd.py
- MEM-02: UTF-8 BOM decoding in frontmatter.py:read and attention build_index / query
- MEM-03: Atomic index.npz generation and cleanup on failure in build_index.py
- MEM-04: Corrupted / truncated index.npz diagnostics and handling in query.py
- MEM-06: Lesson slug prefix normalization in lesson_cmd.py
"""
import argparse
import codecs
import importlib.machinery
import os
import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from commontrace import frontmatter, paths
from commontrace.commands import lesson_cmd, trace_cmd


def _ensure_mock_st(monkeypatch):
    if "sentence_transformers" not in sys.modules:
        mock_st = types.ModuleType("sentence_transformers")
        mock_st.__spec__ = importlib.machinery.ModuleSpec("sentence_transformers", loader=None)
        mock_st.SentenceTransformer = MagicMock()
        monkeypatch.setitem(sys.modules, "sentence_transformers", mock_st)


# ==============================================================================
# SEC-04: Atomic File Writes in frontmatter.py:write
# ==============================================================================
class TestAtomicFileWrites:
    """SEC-04: Verify atomic file writes, overwrite safety, and cleanup on failure."""

    def test_write_creates_file_atomically(self, tmp_path):
        target_file = tmp_path / "lessons" / "lesson_atomic.md"
        fm = {"name": "lesson_atomic", "importance": 4, "status": "active"}
        body = "## Rule\nTest atomic rule\n"

        frontmatter.write(str(target_file), fm, body)

        assert target_file.exists()
        read_fm, read_body = frontmatter.read(str(target_file))
        assert read_fm["name"] == "lesson_atomic"
        assert read_fm["importance"] == 4
        assert "## Rule" in read_body

    def test_write_overwrites_existing_file_atomically(self, tmp_path):
        target_file = tmp_path / "lesson_overwrite.md"
        target_file.write_text("---\nname: original\n---\nOld content\n", encoding="utf-8")

        new_fm = {"name": "updated", "status": "active"}
        new_body = "New body content"

        frontmatter.write(str(target_file), new_fm, new_body)

        read_fm, read_body = frontmatter.read(str(target_file))
        assert read_fm["name"] == "updated"
        assert read_body.strip() == "New body content"

    def test_write_cleans_up_temp_file_on_write_failure(self, tmp_path):
        target_file = tmp_path / "lesson_fail.md"
        target_file.write_text("---\nname: original\n---\nOriginal content\n", encoding="utf-8")

        # Mock exception right after temp file creation before replace
        with patch("yaml.safe_dump", side_effect=ValueError("Serialization error")):
            with pytest.raises(ValueError, match="Serialization error"):
                frontmatter.write(str(target_file), {"name": "fail"}, "body")

        # Original file should be untouched
        assert target_file.read_text(encoding="utf-8") == "---\nname: original\n---\nOriginal content\n"

        # No temporary files left behind in tmp_path
        tmp_files = [f for f in os.listdir(tmp_path) if f.startswith("tmp") or f.endswith(".tmp")]
        assert len(tmp_files) == 0

    def test_write_cleans_up_temp_file_on_rename_failure(self, tmp_path):
        target_file = tmp_path / "lesson_rename_fail.md"

        with patch("os.replace", side_effect=OSError("Rename permission denied")):
            with pytest.raises(OSError, match="Rename permission denied"):
                frontmatter.write(str(target_file), {"name": "fail"}, "body")

        # No temporary files left in tmp_path
        tmp_files = [f for f in os.listdir(tmp_path) if f.startswith("tmp") or f.endswith(".tmp")]
        assert len(tmp_files) == 0


# ==============================================================================
# SEC-05: Resilient Validation Loops in lesson_cmd.py and trace_cmd.py
# ==============================================================================
class TestValidationLoopResilience:
    """SEC-05: Validation loop must process all files despite corrupted/malformed files."""

    def test_lesson_validate_processes_all_files_across_corrupted_frontmatter(
        self, tmp_path, capsys
    ):
        ldir = paths.lessons_dir(str(tmp_path))
        os.makedirs(ldir, exist_ok=True)

        valid_fm = {
            "name": "lesson_1",
            "description": "valid lesson",
            "tags": ["testing"],
            "agent_type": "code",
            "domain": "testing",
            "importance": 3,
            "importance_rationale": "rationale",
            "importance_history": [],
            "applies_when": "when valid",
            "do_not_apply_when": "never",
            "uses": 0,
            "last_hit": "NEVER",
            "source_traces": [],
            "source_episodes": [],
            "status": "active",
        }

        # 1. Valid lesson
        frontmatter.write(os.path.join(ldir, "lesson_01.md"), valid_fm, "## Rule\nValid\n")
        # 2. Corrupted YAML syntax
        with open(os.path.join(ldir, "lesson_02.md"), "w", encoding="utf-8") as f:
            f.write("---\n[unclosed list yaml\n---\nbody\n")
        # 3. Valid lesson
        frontmatter.write(
            os.path.join(ldir, "lesson_03.md"), dict(valid_fm, name="lesson_3"), "## Rule\nValid 3\n"
        )
        # 4. Invalid schema (importance out of bounds)
        frontmatter.write(
            os.path.join(ldir, "lesson_04.md"), dict(valid_fm, name="lesson_4", importance=999), "## Rule\nInvalid\n"
        )
        # 5. Valid lesson
        frontmatter.write(
            os.path.join(ldir, "lesson_05.md"), dict(valid_fm, name="lesson_5"), "## Rule\nValid 5\n"
        )

        args = argparse.Namespace(path=None, dest=str(tmp_path))
        rc = lesson_cmd.run_validate(args)

        assert rc == 1  # non-zero because of errors

        captured = capsys.readouterr()
        output = captured.out

        # Must have checked all 5 files
        assert "OK   " in output and "lesson_01.md" in output
        assert "FAIL " in output and "lesson_02.md" in output
        assert "OK   " in output and "lesson_03.md" in output
        assert "FAIL " in output and "lesson_04.md" in output
        assert "OK   " in output and "lesson_05.md" in output
        assert "3/5 lessons valid" in output

    def test_trace_validate_processes_all_files_across_corrupted_traces(
        self, tmp_path, capsys
    ):
        tdir = paths.traces_dir(str(tmp_path))
        os.makedirs(tdir, exist_ok=True)

        valid_trace_fm = {
            "id": "11111111-1111-4111-8111-111111111111",
            "title": "Valid Trace 1",
            "agent_type": "code",
            "tags": ["test"],
            "profile": "double-review",
        }
        trace_body = "## Context\nTest context\n\n## Solution\nTest solution\n"

        # 1. Valid trace
        frontmatter.write(os.path.join(tdir, "trace_01.md"), valid_trace_fm, trace_body)
        # 2. Corrupted YAML syntax
        with open(os.path.join(tdir, "trace_02.md"), "w", encoding="utf-8") as f:
            f.write("---\n{broken yaml: missing closing\n---\nbody\n")
        # 3. Valid trace
        valid_trace_fm2 = dict(valid_trace_fm, id="22222222-2222-4222-8222-222222222222", title="Valid Trace 2")
        frontmatter.write(os.path.join(tdir, "trace_03.md"), valid_trace_fm2, trace_body)

        args = argparse.Namespace(path=None, dest=str(tmp_path))
        rc = trace_cmd.run_validate(args)

        assert rc == 1

        captured = capsys.readouterr()
        output = captured.out

        assert "OK   " in output and "trace_01.md" in output
        assert "FAIL " in output and "trace_02.md" in output
        assert "OK   " in output and "trace_03.md" in output
        assert "2/3 traces valid" in output

    def test_lesson_validate_processes_all_files_across_binary_invalid_utf8(
        self, tmp_path, capsys
    ):
        ldir = paths.lessons_dir(str(tmp_path))
        os.makedirs(ldir, exist_ok=True)

        valid_fm = {
            "name": "lesson_1",
            "description": "valid lesson",
            "tags": ["testing"],
            "agent_type": "code",
            "domain": "testing",
            "importance": 3,
            "importance_rationale": "rationale",
            "importance_history": [],
            "applies_when": "when valid",
            "do_not_apply_when": "never",
            "uses": 0,
            "last_hit": "NEVER",
            "source_traces": [],
            "source_episodes": [],
            "status": "active",
        }

        # 1. Valid lesson
        frontmatter.write(os.path.join(ldir, "lesson_01.md"), valid_fm, "## Rule\nValid 1\n")
        # 2. Binary / Invalid UTF-8 file
        with open(os.path.join(ldir, "lesson_02_bad.md"), "wb") as f:
            f.write(b"\xff\xfe\x80\x81\xfe\xff")
        # 3. Valid lesson
        frontmatter.write(
            os.path.join(ldir, "lesson_03.md"), dict(valid_fm, name="lesson_3"), "## Rule\nValid 3\n"
        )

        args = argparse.Namespace(path=None, dest=str(tmp_path))
        rc = lesson_cmd.run_validate(args)

        assert rc == 1  # non-zero because of errors

        captured = capsys.readouterr()
        output = captured.out

        assert "OK   " in output and "lesson_01.md" in output
        assert "FAIL " in output and "lesson_02_bad.md" in output
        assert "cannot read" in output or "UnicodeDecodeError" in output
        assert "OK   " in output and "lesson_03.md" in output
        assert "2/3 lessons valid" in output

    def test_trace_validate_processes_all_files_across_binary_invalid_utf8(
        self, tmp_path, capsys
    ):
        tdir = paths.traces_dir(str(tmp_path))
        os.makedirs(tdir, exist_ok=True)

        valid_trace_fm = {
            "id": "11111111-1111-4111-8111-111111111111",
            "title": "Valid Trace 1",
            "agent_type": "code",
            "tags": ["test"],
            "profile": "double-review",
        }
        trace_body = "## Context\nTest context\n\n## Solution\nTest solution\n"

        # 1. Valid trace
        frontmatter.write(os.path.join(tdir, "trace_01.md"), valid_trace_fm, trace_body)
        # 2. Binary / Invalid UTF-8 file
        with open(os.path.join(tdir, "trace_02_bad.md"), "wb") as f:
            f.write(b"\x80\x81\x82\xff\xfe")
        # 3. Valid trace
        valid_trace_fm2 = dict(valid_trace_fm, id="22222222-2222-4222-8222-222222222222", title="Valid Trace 2")
        frontmatter.write(os.path.join(tdir, "trace_03.md"), valid_trace_fm2, trace_body)

        args = argparse.Namespace(path=None, dest=str(tmp_path))
        rc = trace_cmd.run_validate(args)

        assert rc == 1

        captured = capsys.readouterr()
        output = captured.out

        assert "OK   " in output and "trace_01.md" in output
        assert "FAIL " in output and "trace_02_bad.md" in output
        assert "OK   " in output and "trace_03.md" in output
        assert "2/3 traces valid" in output


# ==============================================================================
# MEM-02: UTF-8 BOM Decoding in frontmatter.py, build_index.py & query.py
# ==============================================================================
class TestUtf8BomHandling:
    """MEM-02: Files with UTF-8 BOM (\\ufeff) must decode transparently."""

    def test_frontmatter_read_strips_utf8_bom(self, tmp_path):
        bom_file = tmp_path / "bom_lesson.md"
        raw_bytes = codecs.BOM_UTF8 + b"---\nname: bom_lesson\nimportance: 5\nstatus: active\n---\n## Rule\nBOM Rule\n"
        bom_file.write_bytes(raw_bytes)

        fm, body = frontmatter.read(str(bom_file))
        assert fm["name"] == "bom_lesson"
        assert fm["importance"] == 5
        assert fm["status"] == "active"
        assert "## Rule" in body

    def test_attention_build_index_iter_active_lessons_handles_bom(self, tmp_path, monkeypatch):
        _ensure_mock_st(monkeypatch)
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "attention")
        )
        import build_index

        lessons_dir = tmp_path / "lessons"
        lessons_dir.mkdir(parents=True, exist_ok=True)
        bom_file = lessons_dir / "lesson_bom.md"

        content = (
            "---\n"
            "name: lesson_bom\n"
            "description: BOM test lesson\n"
            "domain: testing\n"
            "tags: [bom, utf8]\n"
            "applies_when: When BOM is present\n"
            "do_not_apply_when: never\n"
            "status: active\n"
            "---\n"
            "## Rule\n"
            "Strip BOM transparently.\n"
        )
        bom_file.write_text(content, encoding="utf-8-sig")

        lessons = list(build_index.iter_active_lessons(str(lessons_dir)))
        assert len(lessons) == 1
        slug, query_text = lessons[0]
        assert slug == "lesson_bom"
        assert "BOM test lesson" in query_text
        assert "Strip BOM transparently." in query_text

    def test_attention_query_load_importances_handles_bom(self, tmp_path, monkeypatch):
        _ensure_mock_st(monkeypatch)
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "attention")
        )
        import query

        lessons_dir = tmp_path / "memory" / "lessons"
        lessons_dir.mkdir(parents=True, exist_ok=True)
        bom_file = lessons_dir / "lesson_imp_bom.md"

        content = (
            "---\n"
            "name: lesson_imp_bom\n"
            "importance: 5\n"
            "status: active\n"
            "---\n"
            "body\n"
        )
        bom_file.write_text(content, encoding="utf-8-sig")

        monkeypatch.setattr(query, "LESSONS_DIR", str(lessons_dir))
        importances, n_parsed = query.load_importances()

        assert importances.get("lesson_imp_bom") == 5
        assert n_parsed == 1


# ==============================================================================
# MEM-03: Atomic index.npz Generation in build_index.py
# ==============================================================================
class TestAtomicIndexGeneration:
    """MEM-03: build_index.py writes index.npz atomically via temp file + os.replace."""

    def test_build_index_preserves_existing_index_and_cleans_tmp_on_error(
        self, tmp_path, monkeypatch
    ):
        _ensure_mock_st(monkeypatch)
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "attention")
        )
        import build_index

        lessons_dir = tmp_path / "memory" / "lessons"
        attention_dir = tmp_path / "memory" / "attention"
        lessons_dir.mkdir(parents=True, exist_ok=True)
        attention_dir.mkdir(parents=True, exist_ok=True)

        lesson_file = lessons_dir / "lesson_01.md"
        lesson_file.write_text(
            "---\nname: lesson_01\ndescription: d\ndomain: t\nstatus: active\n---\n## Rule\nr\n",
            encoding="utf-8",
        )

        index_file = attention_dir / "index.npz"
        # Write pre-existing index
        np.savez(
            str(index_file),
            slugs=np.array(["original_slug"]),
            embeddings=np.zeros((1, 768), dtype=np.float32),
            model_name=np.array("multi-qa-mpnet-base-dot-v1"),
            encoded_field=np.array("fields"),
            timestamp=np.array("2026-01-01T00:00:00"),
            n_lessons=np.array(1),
        )

        monkeypatch.setattr(build_index, "LESSONS_DIR", str(lessons_dir))
        monkeypatch.setattr(build_index, "INDEX_PATH", str(index_file))
        monkeypatch.setattr(sys, "argv", ["build_index.py", "--force"])

        # Simulate exception during np.savez
        with patch("numpy.savez", side_effect=IOError("Simulated disk error during savez")):
            with pytest.raises(IOError, match="Simulated disk error"):
                build_index.main()

        # Original index.npz must still exist and be intact
        assert index_file.exists()
        with np.load(str(index_file), allow_pickle=False) as data:
            assert list(data["slugs"]) == ["original_slug"]

        # Temp file .tmp.npz must be cleaned up
        tmp_files = [f for f in os.listdir(attention_dir) if f.endswith(".tmp.npz")]
        assert len(tmp_files) == 0


# ==============================================================================
# MEM-04: Corrupted / Truncated index.npz Handling in query.py
# ==============================================================================
class TestCorruptedIndexHandling:
    """MEM-04: query.py handles corrupted or truncated index.npz cleanly."""

    @pytest.mark.parametrize(
        "corrupted_content",
        [
            b"",  # empty 0-byte file
            b"PK\x03\x04truncated_garbage_data",
            b"not_a_zip_file_at_all",
        ],
    )
    def test_query_main_exits_cleanly_on_corrupted_index(
        self, tmp_path, monkeypatch, capsys, corrupted_content
    ):
        _ensure_mock_st(monkeypatch)
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "attention")
        )
        import query

        attention_dir = tmp_path / "memory" / "attention"
        attention_dir.mkdir(parents=True, exist_ok=True)
        index_file = attention_dir / "index.npz"
        index_file.write_bytes(corrupted_content)

        monkeypatch.setattr(query, "INDEX_PATH", str(index_file))
        monkeypatch.setattr(sys, "argv", ["query.py", "test query"])

        rc = query.main()
        assert rc == 1

        captured = capsys.readouterr()
        assert "[ERR] Index file at" in captured.err
        assert "is corrupted" in captured.err
        assert "build_index.py --force" in captured.err

    def test_query_main_handles_missing_keys_in_npz_archive(
        self, tmp_path, monkeypatch, capsys
    ):
        _ensure_mock_st(monkeypatch)
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "attention")
        )
        import query

        attention_dir = tmp_path / "memory" / "attention"
        attention_dir.mkdir(parents=True, exist_ok=True)
        index_file = attention_dir / "index.npz"

        # Save an npz with missing keys (e.g. only embeddings, missing model_name / slugs / n_lessons)
        np.savez(str(index_file), embeddings=np.zeros((1, 768), dtype=np.float32))

        monkeypatch.setattr(query, "INDEX_PATH", str(index_file))
        monkeypatch.setattr(sys, "argv", ["query.py", "test query"])

        rc = query.main()
        assert rc == 1

        captured = capsys.readouterr()
        assert "[ERR] Index file at" in captured.err
        assert "is corrupted" in captured.err
        assert "build_index.py --force" in captured.err


# ==============================================================================
# MEM-06: Lesson Slug Prefix Invariant in lesson_cmd.py
# ==============================================================================
class TestLessonSlugPrefixNormalization:
    """MEM-06: Ensure lesson filenames always have lesson_ prefix without duplicating."""

    def test_lesson_new_adds_lesson_prefix_when_absent(self, tmp_path):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        lesson_cmd.add_parser(subparsers)

        args = parser.parse_args([
            "lesson", "new",
            "--slug", "my_custom_rule",
            "--description", "desc",
            "--domain", "refactor",
            "--dest", str(tmp_path),
        ])
        rc = lesson_cmd.run_new(args)
        assert rc == 0

        created_file = tmp_path / "memory" / "lessons" / "lesson_my_custom_rule.md"
        assert created_file.exists()
        # Should not create my_custom_rule.md without prefix
        assert not (tmp_path / "memory" / "lessons" / "my_custom_rule.md").exists()

    def test_lesson_new_preserves_single_lesson_prefix_when_present(self, tmp_path):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        lesson_cmd.add_parser(subparsers)

        args = parser.parse_args([
            "lesson", "new",
            "--slug", "lesson_already_prefixed",
            "--description", "desc",
            "--domain", "refactor",
            "--dest", str(tmp_path),
        ])
        rc = lesson_cmd.run_new(args)
        assert rc == 0

        created_file = tmp_path / "memory" / "lessons" / "lesson_already_prefixed.md"
        assert created_file.exists()
        # Should not double prefix to lesson_lesson_already_prefixed.md
        assert not (tmp_path / "memory" / "lessons" / "lesson_lesson_already_prefixed.md").exists()

    def test_resolve_lesson_path_resolves_both_slug_formats(self, tmp_path):
        ldir = paths.lessons_dir(str(tmp_path))
        os.makedirs(ldir, exist_ok=True)

        lesson_path = os.path.join(ldir, "lesson_test_slug.md")
        frontmatter.write(
            lesson_path,
            {"name": "test_slug", "status": "review"},
            "## Rule\nTest\n",
        )

        resolved_with_prefix = lesson_cmd._resolve_lesson_path(str(tmp_path), "lesson_test_slug")
        resolved_without_prefix = lesson_cmd._resolve_lesson_path(str(tmp_path), "test_slug")

        assert resolved_with_prefix == lesson_path
        assert resolved_without_prefix == lesson_path

    def test_lesson_approve_and_reject_work_with_or_without_prefix(self, tmp_path):
        ldir = paths.lessons_dir(str(tmp_path))
        os.makedirs(ldir, exist_ok=True)

        lesson_path1 = os.path.join(ldir, "lesson_to_approve.md")
        frontmatter.write(
            lesson_path1,
            {"name": "to_approve", "status": "review"},
            "## Rule\nRule\n",
        )

        # Approve using slug without prefix
        args_approve = argparse.Namespace(slug="to_approve", rationale="Good rule", dest=str(tmp_path))
        rc_app = lesson_cmd.run_approve(args_approve)
        assert rc_app == 0
        fm_app, body_app = frontmatter.read(lesson_path1)
        assert fm_app["status"] == "active"
        assert "## Approved" in body_app

        lesson_path2 = os.path.join(ldir, "lesson_to_reject.md")
        frontmatter.write(
            lesson_path2,
            {"name": "to_reject", "status": "review"},
            "## Rule\nRule\n",
        )

        # Reject using slug with prefix
        args_reject = argparse.Namespace(slug="lesson_to_reject", reason="Duplicate", dest=str(tmp_path))
        rc_rej = lesson_cmd.run_reject(args_reject)
        assert rc_rej == 0
        fm_rej, body_rej = frontmatter.read(lesson_path2)
        assert fm_rej["status"] == "archived"
        assert "## Rejected" in body_rej
