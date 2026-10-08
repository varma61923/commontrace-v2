from __future__ import annotations

import importlib
import importlib.machinery
import os
import sys
import types
from unittest.mock import patch

import pytest

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


from commontrace import frontmatter, paths, trace_io
from commontrace.cli import main as cli_main


@pytest.fixture
def clean_store(tmp_path):
    cli_main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
    return tmp_path


@pytest.fixture
def attention_modules():
    if not HAS_NUMPY:
        pytest.skip("numpy not installed")
    had_st = "sentence_transformers" in sys.modules

    if not had_st:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            mock_st = types.ModuleType("sentence_transformers")
            mock_st.__spec__ = importlib.machinery.ModuleSpec("sentence_transformers", None)
            class DummySentenceTransformer:
                def __init__(self, model_name=None):
                    self.model_name = model_name
                def encode(self, texts, **kwargs):
                    if isinstance(texts, str):
                        return np.zeros(384, dtype=np.float32)
                    return np.zeros((len(texts), 384), dtype=np.float32)
            mock_st.SentenceTransformer = DummySentenceTransformer
            sys.modules["sentence_transformers"] = mock_st

    import build_index
    import query

    yield build_index, query

    if not had_st and "sentence_transformers" in sys.modules:
        del sys.modules["sentence_transformers"]


class TestUtf8BomHandling:
    def test_frontmatter_read_with_utf8_bom(self, tmp_path):
        file_path = tmp_path / "lesson_bom.md"
        raw_bytes = (
            b"\xef\xbb\xbf---\n"
            b"name: lesson_bom\n"
            b"description: Testing UTF-8 BOM header\n"
            b"agent_type: code\n"
            b"domain: testing\n"
            b"importance: 4\n"
            b"importance_rationale: BOM must be stripped\n"
            b"applies_when: file has BOM\n"
            b"do_not_apply_when: file does not\n"
            b"uses: 0\n"
            b"last_hit: NEVER\n"
            b"source_traces: []\n"
            b"status: active\n"
            b"tags: [bom, utf8]\n"
            b"---\n\n"
            b"## Rule\n"
            b"Always handle BOM cleanly.\n"
        )
        file_path.write_bytes(raw_bytes)

        fm, body = frontmatter.read(str(file_path))
        assert fm["name"] == "lesson_bom"
        assert fm["description"] == "Testing UTF-8 BOM header"
        assert fm["importance"] == 4
        assert fm["tags"] == ["bom", "utf8"]
        assert "## Rule" in body
        assert "Always handle BOM cleanly." in body

    def test_frontmatter_read_bom_with_crlf_and_unicode(self, tmp_path):
        file_path = tmp_path / "lesson_unicode_bom.md"
        raw_bytes = (
            b"\xef\xbb\xbf---\r\n"
            b"name: lesson_unicode_bom\r\n"
            b"description: \"\xe2\x9c\xa8 Unicode test: \xe4\xb8\xad\xe6\x96\x87 & \xc3\xa9l\xc3\xa9phant\"\r\n"
            b"agent_type: code\r\n"
            b"domain: testing\r\n"
            b"importance: 5\r\n"
            b"importance_rationale: Unicode support check\r\n"
            b"applies_when: unicode used\r\n"
            b"do_not_apply_when: never\r\n"
            b"uses: 0\r\n"
            b"last_hit: NEVER\r\n"
            b"source_traces: []\r\n"
            b"status: active\r\n"
            b"tags: [unicode, emoji]\r\n"
            b"---\r\n\r\n"
            b"## Rule\r\n"
            b"\xf0\x9f\x9a\x80 Launch with unicode support.\r\n"
        )
        file_path.write_bytes(raw_bytes)

        fm, body = frontmatter.read(str(file_path))
        assert fm["name"] == "lesson_unicode_bom"
        assert "✨ Unicode test: 中文 & éléphant" in fm["description"]
        assert "🚀 Launch with unicode support." in body

    def test_trace_io_read_with_utf8_bom(self, tmp_path):
        trace_path = tmp_path / "trace_bom.md"
        raw_bytes = (
            b"\xef\xbb\xbf---\n"
            b"id: trc_bom_01\n"
            b"title: BOM Trace\n"
            b"agent_type: code\n"
            b"tags: [bom, trace]\n"
            b"profile: code-review\n"
            b"outcome:\n"
            b"  resolved: true\n"
            b"---\n\n"
            b"## Context\n"
            b"Context with BOM\n\n"
            b"## Solution\n"
            b"Solution with BOM\n"
        )
        trace_path.write_bytes(raw_bytes)

        instance, body = trace_io.read(str(trace_path))
        assert instance["id"] == "trc_bom_01"
        assert instance["title"] == "BOM Trace"
        assert instance["context_text"] == "Context with BOM"
        assert instance["solution_text"] == "Solution with BOM"
        assert instance["outcome"]["resolved"] is True

    def test_build_index_and_query_with_bom_lessons(self, clean_store, monkeypatch, attention_modules):
        build_index, query = attention_modules
        ldir = paths.lessons_dir(str(clean_store))
        os.makedirs(ldir, exist_ok=True)

        lesson_1 = os.path.join(ldir, "lesson_bom_active.md")
        with open(lesson_1, "wb") as fh:
            fh.write(
                b"\xef\xbb\xbf---\n"
                b"name: lesson_bom_active\n"
                b"description: BOM active lesson for semantic attention\n"
                b"agent_type: code\n"
                b"domain: attention\n"
                b"importance: 5\n"
                b"importance_rationale: Critical index check\n"
                b"applies_when: querying for attention indexing\n"
                b"do_not_apply_when: never\n"
                b"uses: 0\n"
                b"last_hit: NEVER\n"
                b"source_traces: []\n"
                b"status: active\n"
                b"tags: [attention, indexing]\n"
                b"---\n\n"
                b"## Rule\n"
                b"Embed active lessons regardless of BOM header.\n"
            )

        active_lessons = list(build_index.iter_active_lessons(ldir))
        slugs = [slug for slug, _, _ in active_lessons]
        assert "lesson_bom_active" in slugs

        monkeypatch.setattr(query, "LESSONS_DIR", ldir)
        importances, n_parsed = query.load_importances()
        assert importances.get("lesson_bom_active") == 5
        assert n_parsed >= 1


class TestAtomicWritingAndCleanup:
    def test_frontmatter_write_leaves_no_temp_files_on_success(self, tmp_path):
        out_file = tmp_path / "clean_write.md"
        frontmatter.write(
            str(out_file),
            {"name": "clean", "importance": 3},
            "Clean body content\n",
        )
        assert out_file.exists()
        fm, body = frontmatter.read(str(out_file))
        assert fm["name"] == "clean"
        assert body.strip() == "Clean body content"

        all_files = list(tmp_path.iterdir())
        assert all_files == [out_file]

    def test_frontmatter_write_cleans_up_temp_on_write_interruption(self, tmp_path):
        target_file = tmp_path / "protected_file.md"
        target_file.write_text("ORIGINAL_SAFE_CONTENT", encoding="utf-8")

        with patch("yaml.safe_dump", side_effect=IOError("Simulated write interruption")):
            with pytest.raises(IOError, match="Simulated write interruption"):
                frontmatter.write(
                    str(target_file),
                    {"name": "corrupt_attempt"},
                    "new body",
                )

        assert target_file.read_text(encoding="utf-8") == "ORIGINAL_SAFE_CONTENT"
        files_in_dir = list(tmp_path.iterdir())
        assert files_in_dir == [target_file], f"Leftover temp files found: {files_in_dir}"

    def test_frontmatter_write_cleans_up_temp_on_replace_failure(self, tmp_path):
        target_file = tmp_path / "replace_target.md"
        target_file.write_text("ORIGINAL_BEFORE_REPLACE", encoding="utf-8")

        with patch("os.replace", side_effect=PermissionError("Simulated replace lock")):
            with pytest.raises(PermissionError, match="Simulated replace lock"):
                frontmatter.write(
                    str(target_file),
                    {"name": "fail_on_replace"},
                    "new body",
                )

        assert target_file.read_text(encoding="utf-8") == "ORIGINAL_BEFORE_REPLACE"
        files_in_dir = list(tmp_path.iterdir())
        assert files_in_dir == [target_file], f"Leftover temp files found: {files_in_dir}"

    def test_frontmatter_write_cleans_up_on_base_exception(self, tmp_path):
        target_file = tmp_path / "keyboard_target.md"
        target_file.write_text("ORIGINAL_KEYBOARD", encoding="utf-8")

        with patch("os.replace", side_effect=KeyboardInterrupt("Simulated Ctrl+C")):
            with pytest.raises(KeyboardInterrupt):
                frontmatter.write(
                    str(target_file),
                    {"name": "fail_ctrl_c"},
                    "new body",
                )

        assert target_file.read_text(encoding="utf-8") == "ORIGINAL_KEYBOARD"
        files_in_dir = list(tmp_path.iterdir())
        assert files_in_dir == [target_file], f"Leftover temp files found: {files_in_dir}"

    def test_build_index_atomic_write_and_cleanup(self, tmp_path, monkeypatch, attention_modules):
        build_index, _ = attention_modules
        encoder = types.SimpleNamespace(encode=lambda texts, **kwargs:
                                        np.zeros((len(texts), build_index.EMBEDDING_DIM), dtype=np.float32))
        monkeypatch.setattr(build_index, "SentenceTransformer", lambda *args, **kwargs: encoder)
        index_path = tmp_path / "index.npz"

        np.savez(str(index_path), slugs=np.array(["test"]), embeddings=np.zeros((1, 4)))
        assert index_path.exists()

        monkeypatch.setattr(build_index, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(build_index, "LESSONS_DIR", str(tmp_path))

        lesson_file = tmp_path / "lesson_idx.md"
        lesson_file.write_text(
            "---\nname: lesson_idx\ndescription: d\nagent_type: code\ndomain: o\n"
            "importance: 3\nimportance_rationale: r\napplies_when: w\ndo_not_apply_when: n\n"
            "uses: 0\nlast_hit: NEVER\nsource_traces: []\nstatus: active\ntags: []\n---\n\n## Rule\nr\n",
            encoding="utf-8",
        )

        with patch("sys.argv", ["build_index.py", "--force"]):
            with patch("os.replace", side_effect=PermissionError("Simulated index replace failure")):
                with pytest.raises(PermissionError):
                    build_index.main()

        leftover = list(tmp_path.glob("*.tmp.npz"))
        assert leftover == [], f"Temporary index files were not cleaned up: {leftover}"
        assert index_path.exists()


class TestBatchValidationResilience:
    def test_lesson_validate_batch_continues_past_malformed_and_invalid_files(self, clean_store, capsys):
        ldir = paths.lessons_dir(str(clean_store))

        def make_lesson(filename, content):
            p = os.path.join(ldir, filename)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
            return p

        make_lesson(
            "lesson_01_valid.md",
            "---\nname: lesson_01\ndescription: valid 1\nagent_type: code\ndomain: testing\n"
            "importance: 3\nimportance_rationale: r\napplies_when: w\ndo_not_apply_when: n\n"
            "uses: 0\nlast_hit: NEVER\nsource_traces: []\nstatus: active\ntags: []\n---\n\n## Rule\nr1\n",
        )

        make_lesson(
            "lesson_02_bad_yaml.md",
            "---\nname: lesson_02\nkey: [unclosed bracket\n---\n\nbody\n",
        )

        make_lesson(
            "lesson_03_valid.md",
            "---\nname: lesson_03\ndescription: valid 3\nagent_type: code\ndomain: testing\n"
            "importance: 4\nimportance_rationale: r\napplies_when: w\ndo_not_apply_when: n\n"
            "uses: 0\nlast_hit: NEVER\nsource_traces: []\nstatus: active\ntags: []\n---\n\n## Rule\nr3\n",
        )

        make_lesson(
            "lesson_04_scalar.md",
            "---\n\"just a raw scalar string\"\n---\n\nbody\n",
        )

        make_lesson(
            "lesson_05_schema_err.md",
            "---\nname: lesson_05\ndescription: d\nagent_type: code\ndomain: testing\n"
            "importance: 99\nimportance_rationale: r\napplies_when: w\ndo_not_apply_when: n\n"
            "uses: 0\nlast_hit: NEVER\nsource_traces: []\nstatus: active\ntags: []\n---\n\n## Rule\nr5\n",
        )

        make_lesson(
            "lesson_06_valid.md",
            "---\nname: lesson_06\ndescription: valid 6\nagent_type: code\ndomain: testing\n"
            "importance: 2\nimportance_rationale: r\napplies_when: w\ndo_not_apply_when: n\n"
            "uses: 0\nlast_hit: NEVER\nsource_traces: []\nstatus: active\ntags: []\n---\n\n## Rule\nr6\n",
        )

        capsys.readouterr()
        rc = cli_main(["lesson", "validate", "--dest", str(clean_store)])
        out = capsys.readouterr().out

        assert rc == 1
        assert "OK   " in out
        assert "FAIL " in out
        assert "lesson_01_valid.md" in out
        assert "lesson_02_bad_yaml.md" in out
        assert "lesson_03_valid.md" in out
        assert "lesson_04_scalar.md" in out
        assert "lesson_05_schema_err.md" in out
        assert "lesson_06_valid.md" in out
        assert "3/6 lessons valid" in out

    def test_trace_validate_batch_continues_past_malformed_and_invalid_files(self, clean_store, capsys):
        tdir = paths.traces_dir(str(clean_store))

        def make_trace(filename, content):
            p = os.path.join(tdir, filename)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
            return p

        make_trace(
            "trace_01_valid.md",
            "---\nid: trc_1\ntitle: Valid Trace 1\nagent_type: code\ntags: []\nprofile: code-review\n"
            "outcome:\n  resolved: true\n---\n\n## Context\nc1\n\n## Solution\ns1\n",
        )

        make_trace(
            "trace_02_bad_yaml.md",
            "---\nid: trc_2\nmalformed: {unclosed\n---\n\n## Context\nc\n\n## Solution\ns\n",
        )

        make_trace(
            "trace_03_valid.md",
            "---\nid: trc_3\ntitle: Valid Trace 3\nagent_type: code\ntags: []\nprofile: code-review\n"
            "outcome:\n  resolved: false\n---\n\n## Context\nc3\n\n## Solution\ns3\n",
        )

        make_trace(
            "trace_04_scalar.md",
            "---\n12345\n---\n\n## Context\nc\n\n## Solution\ns\n",
        )

        make_trace(
            "trace_05_schema_err.md",
            "---\nid: trc_5\ntitle: Bad Schema\nagent_type: code\ntags: []\nprofile: code-review\n"
            "outcome:\n  resolved: 'not-a-bool'\n---\n\n## Context\nc5\n\n## Solution\ns5\n",
        )

        make_trace(
            "trace_06_valid.md",
            "---\nid: trc_6\ntitle: Valid Trace 6\nagent_type: code\ntags: []\nprofile: code-review\n"
            "outcome:\n  resolved: true\n---\n\n## Context\nc6\n\n## Solution\ns6\n",
        )

        capsys.readouterr()
        rc = cli_main(["trace", "validate", "--dest", str(clean_store)])
        out = capsys.readouterr().out

        assert rc == 1
        assert "trace_01_valid.md" in out
        assert "trace_02_bad_yaml.md" in out
        assert "trace_03_valid.md" in out
        assert "trace_04_scalar.md" in out
        assert "trace_05_schema_err.md" in out
        assert "trace_06_valid.md" in out
        assert "3/6 traces valid" in out

    def test_validation_explicit_missing_file_diagnostic(self, clean_store, capsys):
        capsys.readouterr()
        rc = cli_main(["lesson", "validate", "/nonexistent/path/lesson_none.md", "--dest", str(clean_store)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "cannot read" in err


class TestLessonSlugNormalizationAndIndexing:
    def test_lesson_new_without_prefix_creates_prefixed_file(self, clean_store):
        rc = cli_main(
            [
                "lesson", "new",
                "--slug", "my_custom_slug",
                "--description", "Testing slug normalization",
                "--agent-type", "code",
                "--domain", "testing",
                "--dest", str(clean_store),
            ]
        )
        assert rc == 0
        ldir = paths.lessons_dir(str(clean_store))
        expected_file = os.path.join(ldir, "lesson_my_custom_slug.md")
        assert os.path.isfile(expected_file), f"File {expected_file} does not exist!"

        fm, _ = frontmatter.read(expected_file)
        assert fm["name"] == "my_custom_slug"

    def test_lesson_new_with_prefix_avoids_double_prefix(self, clean_store):
        rc = cli_main(
            [
                "lesson", "new",
                "--slug", "lesson_already_prefixed",
                "--description", "Testing slug already prefixed",
                "--agent-type", "code",
                "--domain", "testing",
                "--dest", str(clean_store),
            ]
        )
        assert rc == 0
        ldir = paths.lessons_dir(str(clean_store))
        expected_file = os.path.join(ldir, "lesson_already_prefixed.md")
        wrong_file = os.path.join(ldir, "lesson_lesson_already_prefixed.md")
        assert os.path.isfile(expected_file)
        assert not os.path.exists(wrong_file)

    def test_slug_resolution_and_approval_workflow(self, clean_store, capsys):
        cli_main(
            [
                "lesson", "new",
                "--slug", "review_candidate",
                "--description", "Candidate for approval",
                "--agent-type", "code",
                "--domain", "testing",
                "--dest", str(clean_store),
            ]
        )
        ldir = paths.lessons_dir(str(clean_store))
        lesson_file = os.path.join(ldir, "lesson_review_candidate.md")

        fm, body = frontmatter.read(lesson_file)
        fm["status"] = "review"
        fm["applies_when"] = "A test asserts on wall-clock time"
        fm["do_not_apply_when"] = "The test is deliberately measuring duration"
        body = (
            "## Rule\nFreeze the clock instead of sleeping.\n\n"
            "## Why\nObserved in three flaky suites.\n\n"
            "## How to apply\nInject a clock and assert on it.\n\n"
            "## Counter-examples\nBenchmarks.\n"
        )
        frontmatter.write(lesson_file, fm, body)

        rc = cli_main([
            "lesson", "approve", "review_candidate", "--rationale", "Looks good", "--dest", str(clean_store),
        ])
        assert rc == 0
        fm, body = frontmatter.read(lesson_file)
        assert fm["status"] == "active"
        assert "Looks good" in body

        fm["status"] = "review"
        frontmatter.write(lesson_file, fm, body)
        rc = cli_main([
            "lesson", "reject", "lesson_review_candidate", "--reason", "Not ready", "--dest", str(clean_store),
        ])
        assert rc == 0
        fm, body = frontmatter.read(lesson_file)
        assert fm["status"] == "archived"
        assert "Not ready" in body

    def test_normalized_lessons_match_indexer_glob(self, clean_store, attention_modules):
        build_index, _ = attention_modules
        cli_main(
            [
                "lesson", "new",
                "--slug", "auto_norm_1",
                "--description", "Auto norm 1",
                "--agent-type", "code",
                "--domain", "testing",
                "--dest", str(clean_store),
            ]
        )
        cli_main(
            [
                "lesson", "new",
                "--slug", "lesson_auto_norm_2",
                "--description", "Auto norm 2",
                "--agent-type", "code",
                "--domain", "testing",
                "--dest", str(clean_store),
            ]
        )

        ldir = paths.lessons_dir(str(clean_store))
        for filename in ("lesson_auto_norm_1.md", "lesson_auto_norm_2.md"):
            path = os.path.join(ldir, filename)
            fm, body = frontmatter.read(path)
            fm["status"] = "active"
            frontmatter.write(path, fm, body)

        active_slugs = [slug for slug, _, _ in build_index.iter_active_lessons(ldir)]
        assert "auto_norm_1" in active_slugs
        assert "lesson_auto_norm_2" in active_slugs


class TestCorruptedIndexDiagnostics:
    def test_query_handles_empty_zero_byte_index(self, tmp_path, monkeypatch, capsys, attention_modules):
        _, query = attention_modules
        idx = tmp_path / "index.npz"
        idx.write_bytes(b"")
        monkeypatch.setattr(query, "INDEX_PATH", str(idx))

        capsys.readouterr()
        with patch("sys.argv", ["query.py", "test query"]):
            rc = query.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "is corrupted" in err
        assert "rebuild the index" in err

    def test_query_handles_corrupted_garbage_index(self, tmp_path, monkeypatch, capsys, attention_modules):
        _, query = attention_modules
        idx = tmp_path / "index.npz"
        idx.write_bytes(b"THIS IS NOT A VALID NPZ OR ZIP FILE AT ALL 1234567890")
        monkeypatch.setattr(query, "INDEX_PATH", str(idx))

        capsys.readouterr()
        with patch("sys.argv", ["query.py", "test query"]):
            rc = query.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "is corrupted" in err
        assert "rebuild the index" in err

    def test_query_handles_truncated_zip_index(self, tmp_path, monkeypatch, capsys, attention_modules):
        _, query = attention_modules
        idx = tmp_path / "index.npz"
        np.savez(str(idx), slugs=np.array(["test"]), embeddings=np.zeros((1, 4)))
        raw = idx.read_bytes()
        idx.write_bytes(raw[: len(raw) // 2])
        monkeypatch.setattr(query, "INDEX_PATH", str(idx))

        capsys.readouterr()
        with patch("sys.argv", ["query.py", "test query"]):
            rc = query.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "is corrupted" in err
