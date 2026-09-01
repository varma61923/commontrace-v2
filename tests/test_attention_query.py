"""Tests for memory/attention/query.py's handling of a tampered index.npz.

index.npz is a local build artifact, but nothing stops one arriving on a machine
via git clone/fork/sync rather than a local `build_index.py` run. Its `model_name`
field must not be trusted blindly -- see the comment in query.py for the CVE
context (a malicious model_name could point SentenceTransformer at an arbitrary,
code-executing Hugging Face Hub repo).
"""
import json
import os
import sys

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sentence_transformers")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "attention"))
import query as attn_query  # noqa: E402 -- must follow importorskip + sys.path.insert above


def _write_index(path, model_name, n=2):
    np.savez(
        path,
        slugs=np.array([f"lesson_{i}" for i in range(n)]),
        embeddings=np.zeros((n, 3), dtype=np.float32),
        model_name=np.array(model_name),
        encoded_field=np.array("x"),
        timestamp=np.array("2026-01-01T00:00:00"),
        n_lessons=np.array(n),
    )


def _write_active_lesson_files(lessons_dir, n):
    """Write real, active lesson_<i>.md files backing the fake index slugs
    _write_index() produces (lesson_0..lesson_{n-1}) -- load_importances()
    reads these from disk directly, and query.py's active-lessons filter
    (see the [BUG-MEM-02] fix) now excludes any index slug that is not
    backed by one of these, so a test asserting on which/how many index
    slugs make it into the retrieved set needs both, not the index alone."""
    for i in range(n):
        (lessons_dir / f"lesson_{i}.md").write_text(
            f"---\nname: lesson_{i}\nimportance: 3\nstatus: active\n---\nbody\n", encoding="utf-8"
        )


class TestTamperedModelName:
    def test_untrusted_model_name_is_rejected_without_loading(self, tmp_path, monkeypatch, capsys):
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), "attacker/malicious-repo")
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))
        monkeypatch.setattr(attn_query, "TELEMETRY_PATH", str(tmp_path / "alpha_telemetry.jsonl"))

        called = []
        monkeypatch.setattr(
            attn_query, "SentenceTransformer", lambda name: called.append(name) or object()
        )
        monkeypatch.setattr(sys, "argv", ["query.py", "some task"])

        rc = attn_query.main()
        assert rc == 1
        assert called == []  # SentenceTransformer must never be constructed
        err = capsys.readouterr().err
        assert "attacker/malicious-repo" in err

    def test_trusted_model_name_still_loads(self, tmp_path, monkeypatch):
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), attn_query._TRUSTED_MODEL_NAME)
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))
        monkeypatch.setattr(attn_query, "TELEMETRY_PATH", str(tmp_path / "alpha_telemetry.jsonl"))

        class FakeModel:
            def encode(self, *a, **k):
                return np.zeros(3, dtype=np.float32)

        called = []
        monkeypatch.setattr(
            attn_query, "SentenceTransformer", lambda name: called.append(name) or FakeModel()
        )
        monkeypatch.setattr(sys, "argv", ["query.py", "some task"])

        rc = attn_query.main()
        assert rc == 0
        assert called == [attn_query._TRUSTED_MODEL_NAME]

    def test_a_network_load_failure_is_a_clean_error_not_a_traceback(self, tmp_path, monkeypatch, capsys):
        """SentenceTransformer downloads the model from Hugging Face Hub on
        first use if it isn't already cached locally -- an air-gapped host,
        or a cache the operator didn't realize was never populated, raises
        a raw OSError from deep inside huggingface_hub. Must surface as the
        same clean [ERR]/return 1 pattern every other failure path here
        uses, not an uncaught traceback."""
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), attn_query._TRUSTED_MODEL_NAME)
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))
        monkeypatch.setattr(attn_query, "TELEMETRY_PATH", str(tmp_path / "alpha_telemetry.jsonl"))

        def _raise_offline(name):
            raise OSError(f"We couldn't connect to 'https://huggingface.co' to load {name}")

        monkeypatch.setattr(attn_query, "SentenceTransformer", _raise_offline)
        monkeypatch.setattr(sys, "argv", ["query.py", "some task"])

        rc = attn_query.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "[ERR]" in err
        assert "HF_HUB_OFFLINE" in err


class TestAlphaTelemetry:
    """Tests for the Phase 3 (P5) operational-cost instrumentation: query.py must append
    one JSON line per invocation to memory/alpha_telemetry.jsonl, creating it if absent and
    never truncating prior history."""

    def _run(self, tmp_path, monkeypatch, query="some task"):
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), attn_query._TRUSTED_MODEL_NAME, n=3)
        _write_active_lesson_files(tmp_path, 3)
        telemetry_path = tmp_path / "alpha_telemetry.jsonl"
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))
        monkeypatch.setattr(attn_query, "TELEMETRY_PATH", str(telemetry_path))

        class FakeModel:
            def encode(self, *a, **k):
                return np.zeros(3, dtype=np.float32)

        monkeypatch.setattr(attn_query, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(sys, "argv", ["query.py", query])
        rc = attn_query.main()
        assert rc == 0
        return telemetry_path

    def test_telemetry_file_created_with_one_record(self, tmp_path, monkeypatch):
        telemetry_path = self._run(tmp_path, monkeypatch)
        assert telemetry_path.exists()
        lines = telemetry_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert set(rec) >= {
            "timestamp", "latency_ms", "n_frontmatters_parsed",
            "n_candidates_surfaced", "estimated_tokens",
        }
        assert isinstance(rec["latency_ms"], (int, float)) and rec["latency_ms"] >= 0
        assert rec["n_candidates_surfaced"] == 3  # all 3 lessons in the tiny fake index
        assert rec["estimated_tokens"] > 0

    def test_telemetry_appends_without_truncating(self, tmp_path, monkeypatch):
        telemetry_path = self._run(tmp_path, monkeypatch, query="first task")
        # Second invocation reuses the same tmp_path/index, must append not overwrite.
        telemetry_path2 = self._run(tmp_path, monkeypatch, query="second task")
        assert telemetry_path == telemetry_path2
        lines = telemetry_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # each line independently valid JSON

    def test_telemetry_creates_parent_dir_if_absent(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "dir"
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), attn_query._TRUSTED_MODEL_NAME, n=1)
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))
        monkeypatch.setattr(attn_query, "TELEMETRY_PATH", str(nested / "alpha_telemetry.jsonl"))

        class FakeModel:
            def encode(self, *a, **k):
                return np.zeros(3, dtype=np.float32)

        monkeypatch.setattr(attn_query, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(sys, "argv", ["query.py", "task"])
        assert attn_query.main() == 0
        assert (nested / "alpha_telemetry.jsonl").exists()

    def test_telemetry_rotates_once_it_crosses_the_size_threshold(self, tmp_path, monkeypatch):
        """One record gets appended per query invocation with no retention
        limit, so a long-lived store's telemetry file grew without bound.
        Rotated to a single .1 backup once it crosses the size threshold
        instead."""
        path = tmp_path / "alpha_telemetry.jsonl"
        monkeypatch.setattr(attn_query, "_TELEMETRY_MAX_BYTES", 100)
        path.write_text("x" * 200, encoding="utf-8")

        attn_query._append_telemetry({"a": 1}, path=str(path))

        assert (tmp_path / "alpha_telemetry.jsonl.1").read_text(encoding="utf-8") == "x" * 200
        assert json.loads(path.read_text(encoding="utf-8").strip()) == {"a": 1}

    def test_telemetry_does_not_rotate_below_the_threshold(self, tmp_path, monkeypatch):
        path = tmp_path / "alpha_telemetry.jsonl"
        monkeypatch.setattr(attn_query, "_TELEMETRY_MAX_BYTES", 100_000)
        path.write_text('{"a": 1}\n', encoding="utf-8")

        attn_query._append_telemetry({"b": 2}, path=str(path))

        assert not (tmp_path / "alpha_telemetry.jsonl.1").exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2


class TestLoadImportancesParsedCount:
    def test_counts_all_parsed_frontmatters_including_inactive(self, tmp_path):
        (tmp_path / "lesson_a.md").write_text(
            "---\nname: lesson_a\nimportance: 3\nstatus: active\n---\nbody\n", encoding="utf-8"
        )
        (tmp_path / "lesson_b.md").write_text(
            "---\nname: lesson_b\nimportance: 4\nstatus: archived\n---\nbody\n", encoding="utf-8"
        )
        (tmp_path / "lesson_template.md").write_text(
            "---\nname: lesson_template\n---\nbody\n", encoding="utf-8"
        )
        # load_importances reads the module-level LESSONS_DIR global directly, so patch it.
        old_dir = attn_query.LESSONS_DIR
        attn_query.LESSONS_DIR = str(tmp_path)
        try:
            importances, n_parsed = attn_query.load_importances()
        finally:
            attn_query.LESSONS_DIR = old_dir
        assert n_parsed == 2  # template excluded, active + archived both counted as "parsed"
        assert importances == {"lesson_a": 3}  # only the active one is retrieval-eligible


class TestLoadImportancesSurvivesUnreadableFile:
    def test_an_unreadable_lesson_is_skipped_not_a_crash(self, tmp_path, monkeypatch):
        """A file matched by glob() can still fail to open() -- permissions,
        deleted out from under us by a concurrent command, a broken symlink.
        Mocked here (rather than chmod 0o000) so the test is deterministic
        regardless of the user running it -- root bypasses permission bits
        entirely, which would make a chmod-based test silently pass for the
        wrong reason."""
        (tmp_path / "lesson_a.md").write_text(
            "---\nname: lesson_a\nimportance: 4\nstatus: active\n---\nbody\n", encoding="utf-8"
        )
        broken_path = str(tmp_path / "lesson_locked.md")
        (tmp_path / "lesson_locked.md").write_text(
            "---\nname: lesson_locked\nimportance: 5\nstatus: active\n---\nbody\n", encoding="utf-8"
        )

        real_open = open

        def _flaky_open(path, *a, **k):
            if str(path) == broken_path:
                raise OSError("permission denied (simulated)")
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", _flaky_open)

        old_dir = attn_query.LESSONS_DIR
        attn_query.LESSONS_DIR = str(tmp_path)
        try:
            importances, n_parsed = attn_query.load_importances()
        finally:
            attn_query.LESSONS_DIR = old_dir
        assert importances == {"lesson_a": 4}
        assert n_parsed == 1


class TestArchivedLessonsExcludedFromTopK:
    """[BUG-MEM-02]: index.npz keeps one row per lesson as of the last
    build_index.py run. A lesson archived (or deleted from disk) since then
    still has a row and a cosine score -- query.py used to rank purely off
    `scores`, so that stale row could still win a top-K slot from an
    actually-active lesson, surfacing in the brief as
    `lesson_x | cosine=0.9xx | importance=0`."""

    def test_an_archived_lesson_in_the_index_never_appears_in_the_brief(
        self, tmp_path, monkeypatch
    ):
        index_path = tmp_path / "index.npz"
        np.savez(
            str(index_path),
            slugs=np.array(["active_one", "archived_one"]),
            embeddings=np.array([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32),
            model_name=np.array(attn_query._TRUSTED_MODEL_NAME),
            encoded_field=np.array("x"),
            timestamp=np.array("2026-01-01T00:00:00"),
            n_lessons=np.array(2),
        )
        (tmp_path / "lesson_active_one.md").write_text(
            "---\nname: active_one\nimportance: 3\nstatus: active\n---\nbody\n", encoding="utf-8"
        )
        # archived_one has NO corresponding active lesson file on disk --
        # exactly the leaked-index state the fix targets. (Whether it's
        # archived, deleted, or simply never re-indexed makes no difference
        # to query.py: load_importances() only ever sees active lessons.)
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))
        monkeypatch.setattr(attn_query, "TELEMETRY_PATH", str(tmp_path / "alpha_telemetry.jsonl"))

        class FakeModel:
            def encode(self, *a, **k):
                return np.array([1.0, 0.0, 0.0], dtype=np.float32)

        monkeypatch.setattr(attn_query, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(sys, "argv", ["query.py", "query text", "--top-k=5", "--include-importance-floor=4"])

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = attn_query.main()
        out = buf.getvalue()
        assert rc == 0
        assert "archived_one" not in out
        assert "active_one" in out


class TestImportanceFloorZeroDisablesOverride:
    """[BUG-MEM-05]: importance is schema-bounded to [1, 5], so
    `--include-importance-floor 0` made `importances.get(slug, 0) >= floor`
    trivially true for every lesson, dumping the entire store into the
    brief instead of respecting --top-k. There was also no other way to
    disable the safety override at all, so floor<=0 is now treated as
    that missing "disabled" sentinel instead."""

    def test_floor_zero_does_not_dump_the_whole_store(self, tmp_path, monkeypatch):
        n = 5
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), attn_query._TRUSTED_MODEL_NAME, n=n)
        _write_active_lesson_files(tmp_path, n)
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))
        monkeypatch.setattr(attn_query, "TELEMETRY_PATH", str(tmp_path / "alpha_telemetry.jsonl"))

        class FakeModel:
            def encode(self, *a, **k):
                return np.zeros(3, dtype=np.float32)

        monkeypatch.setattr(attn_query, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(
            sys, "argv", ["query.py", "query text", "--top-k=2", "--include-importance-floor=0"]
        )

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = attn_query.main()
        out = buf.getvalue()
        assert rc == 0
        result_lines = [line for line in out.splitlines() if line and not line.startswith("#")]
        assert len(result_lines) == 2, f"expected top-k=2 results, got {len(result_lines)}: {result_lines}"
        assert "override disabled" in out


class TestStrictBoolLoaderParity:
    """query.py used plain yaml.safe_load while build_index.py used
    commontrace.frontmatter._StrictBoolLoader -- YAML 1.1's implicit bool
    conversion means a lesson `name: on` (unquoted, exactly what a
    hand-edited lesson file looks like) parses to the string "on" under the
    strict loader but the boolean True under plain safe_load. build_index.py
    keys its index under the strict-loader slug ("on"); this module's
    importance-floor safety override then looked the lesson up under
    `importances[True]` -- a key that can never match the string keys the
    rest of the retrieval pipeline uses -- and silently lost the override
    for exactly the lessons whose names look like a YAML 1.1 bool token."""

    def test_a_yaml_1_1_bool_like_lesson_name_stays_a_string(self, tmp_path):
        (tmp_path / "lesson_on.md").write_text(
            "---\nname: on\nimportance: 5\nstatus: active\n---\nbody\n", encoding="utf-8"
        )
        old_dir = attn_query.LESSONS_DIR
        attn_query.LESSONS_DIR = str(tmp_path)
        try:
            importances, _n_parsed = attn_query.load_importances()
        finally:
            attn_query.LESSONS_DIR = old_dir
        assert importances == {"on": 5}, (
            f"expected the string key 'on', got {importances!r} -- "
            "a plain yaml.safe_load would coerce the name to the bool True instead"
        )
