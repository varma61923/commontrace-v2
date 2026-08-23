"""Tests for commontrace/reference/measure_performance.py's Phase 3 additions:

- P3: run persistence by default + --diff + --history
- P4: alert thresholds (STATUS.md §5 defaults) incl. unimodal importance distribution,
      and --strict exit-code semantics
- P5: Operational Cost section reading memory/alpha_telemetry.jsonl
- P8: Semantic near-duplicates section reading memory/attention/index.npz

Uses tmp directories throughout -- never touches the real repo memory/.
"""
import glob
import importlib.util
import json
import os
import sys

import measure_performance as bm
import pytest

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# Loaded by explicit file path, not `from conftest import ...` -- see
# tests/test_benchmark.py's identical comment for why: hub/tests/ also has
# its own conftest.py, and a bare `import conftest` resolves against
# whichever same-named module pytest's default import mode put on sys.path
# first, which depends on collection order when both test suites run
# together (`pytest tests/ hub/tests/`).
_conftest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py")
_conftest_spec = importlib.util.spec_from_file_location("commontrace_tests_conftest", _conftest_path)
_conftest = importlib.util.module_from_spec(_conftest_spec)
_conftest_spec.loader.exec_module(_conftest)
write_episode = _conftest.write_episode
write_lesson = _conftest.write_lesson


# ---------------------------------------------------------------------------
# P4 -- default thresholds match STATUS.md §5 P4 exactly
# ---------------------------------------------------------------------------

class TestDefaultThresholds:
    def test_defaults_match_status_md_p4(self):
        assert bm.DEFAULT_THRESHOLD_QUALITY == 0.7
        assert bm.DEFAULT_THRESHOLD_RETRIEVAL == 0.5
        assert bm.DEFAULT_THRESHOLD_NEVER_HIT == 0.3
        assert bm.DEFAULT_THRESHOLD_UNIMODAL == 0.95


class TestUnimodalAlert:
    def _make_report(self, importance_lessons):
        return {
            "lesson_quality": {"value": 1.0, "n": 1},
            "implicit_retrieval": {"strict": 1.0, "permissive": 1.0, "n": 1},
            "transfer_gap": {"value": 0.0, "n": 1, "untraceable": 0},
            "n_lessons": sum(importance_lessons.values()),
            "extras": {
                "never_hit": [], "top5": [], "proposed_not_validated": [],
                "importance_lessons": importance_lessons, "importance_episodes": {},
                "domain_coverage": {},
            },
        }

    def _thresholds(self):
        return {
            "quality": bm.DEFAULT_THRESHOLD_QUALITY,
            "retrieval": bm.DEFAULT_THRESHOLD_RETRIEVAL,
            "never_hit": bm.DEFAULT_THRESHOLD_NEVER_HIT,
            "unimodal": bm.DEFAULT_THRESHOLD_UNIMODAL,
        }

    def test_unimodal_distribution_fires_alert(self):
        report = self._make_report({3: 20})  # 100% at importance 3
        alerts = bm.compute_alerts(report, self._thresholds())
        assert any("nimodal" in a for a in alerts)

    def test_balanced_distribution_no_alert(self):
        report = self._make_report({3: 10, 4: 9, 5: 1})  # mirrors real snapshot in STATUS.md
        alerts = bm.compute_alerts(report, self._thresholds())
        assert not any("nimodal" in a for a in alerts)

    def test_just_under_threshold_no_alert(self):
        # 94/100 = 94% < 95% default threshold
        report = self._make_report({3: 94, 4: 6})
        alerts = bm.compute_alerts(report, self._thresholds())
        assert not any("nimodal" in a for a in alerts)

    def test_empty_lessons_no_crash(self):
        report = self._make_report({})
        alerts = bm.compute_alerts(report, self._thresholds())
        assert alerts == []

    def test_unimodal_threshold_is_configurable(self):
        report = self._make_report({3: 8, 4: 2})  # 80%
        thresholds = self._thresholds()
        thresholds["unimodal"] = 0.99
        assert not any("nimodal" in a for a in bm.compute_alerts(report, thresholds))
        thresholds["unimodal"] = 0.5
        assert any("nimodal" in a for a in bm.compute_alerts(report, thresholds))

    def test_missing_unimodal_key_defaults_gracefully(self):
        """compute_alerts must not KeyError when a caller (e.g. existing tests, older
        callers) passes a thresholds dict without the new 'unimodal' key."""
        report = self._make_report({3: 20})
        thresholds = {"quality": 0.8, "retrieval": 0.7, "never_hit": 0.25}
        alerts = bm.compute_alerts(report, thresholds)  # must not raise
        assert any("nimodal" in a for a in alerts)  # falls back to DEFAULT_THRESHOLD_UNIMODAL


# ---------------------------------------------------------------------------
# P3 -- run persistence + --diff + --history
# ---------------------------------------------------------------------------

class TestPersistReport:
    def test_persist_report_writes_json_with_schema_version(self, tmp_memory):
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            path = bm.persist_report({"schema_version": bm.SCHEMA_VERSION, "n_episodes": 1})
        finally:
            bm.BASE_DIR = old_base
        assert os.path.isfile(path)
        with open(path) as fh:
            data = json.load(fh)
        assert data["schema_version"] == bm.SCHEMA_VERSION

    def test_persist_report_filename_pattern(self, tmp_memory):
        import datetime
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            ts = datetime.datetime(2026, 5, 27, 17, 57, 46, 123456)
            path = bm.persist_report({"x": 1}, ts=ts)
        finally:
            bm.BASE_DIR = old_base
        # Microseconds included (L-36): two runs within the same second
        # previously collided on an identical filename and the second
        # silently overwrote the first's report.
        assert os.path.basename(path) == "2026-05-27_175746_123456.json"

    def test_persist_report_does_not_collide_within_the_same_second(self, tmp_memory):
        """Even same microsecond -- pinned explicitly, since two runs
        computed a report fast enough to share one wall-clock tick used to
        silently overwrite each other with no warning at all."""
        import datetime
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            ts = datetime.datetime(2026, 5, 27, 17, 57, 46, 123456)
            first = bm.persist_report({"run": 1}, ts=ts)
            second = bm.persist_report({"run": 2}, ts=ts)
        finally:
            bm.BASE_DIR = old_base
        assert first != second
        with open(first) as fh:
            assert json.load(fh)["run"] == 1
        with open(second) as fh:
            assert json.load(fh)["run"] == 2


class TestLoadStoredReports:
    def test_no_reports(self, tmp_memory):
        assert bm.load_stored_reports(str(tmp_memory / "benchmark_reports")) == []

    def test_skips_unreadable_file(self, tmp_memory, capsys):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-01_000000.json").write_text("not valid json{{{", encoding="utf-8")
        (reports_dir / "2026-01-02_000000.json").write_text(json.dumps({"n_episodes": 1}), encoding="utf-8")
        stored = bm.load_stored_reports(str(reports_dir))
        assert len(stored) == 1
        assert "unreadable" in capsys.readouterr().err.lower()

    def test_sorted_oldest_first(self, tmp_memory):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-02_000000.json").write_text(json.dumps({"tag": "b"}), encoding="utf-8")
        (reports_dir / "2026-01-01_000000.json").write_text(json.dumps({"tag": "a"}), encoding="utf-8")
        stored = bm.load_stored_reports(str(reports_dir))
        assert [r["tag"] for _, r in stored] == ["a", "b"]


def _fake_report(lesson_quality, ir_strict, ir_permissive, transfer_gap):
    return {
        "lesson_quality": {"value": lesson_quality, "n": 1},
        "implicit_retrieval": {"strict": ir_strict, "permissive": ir_permissive, "n": 1},
        "transfer_gap": {"value": transfer_gap, "n": 1, "untraceable": 0},
    }


class TestComputeDiff:
    def test_flags_delta_over_5pp(self):
        older = _fake_report(0.90, 0.80, 0.90, 0.0)
        newer = _fake_report(0.80, 0.80, 0.90, 0.0)  # lesson_quality dropped 10pp
        rows = bm.compute_diff(older, newer)
        lq_row = next(r for r in rows if r["metric"] == "lesson_quality")
        assert lq_row["flagged"] is True
        assert lq_row["delta"] == pytest.approx(-0.10)

    def test_no_flag_under_5pp(self):
        older = _fake_report(0.90, 0.80, 0.90, 0.0)
        newer = _fake_report(0.92, 0.80, 0.90, 0.0)  # +2pp
        rows = bm.compute_diff(older, newer)
        lq_row = next(r for r in rows if r["metric"] == "lesson_quality")
        assert lq_row["flagged"] is False

    def test_none_values_produce_no_delta(self):
        older = _fake_report(None, 0.8, 0.9, None)
        newer = _fake_report(0.9, 0.8, 0.9, None)
        rows = bm.compute_diff(older, newer)
        tg_row = next(r for r in rows if r["metric"] == "transfer_gap")
        assert tg_row["delta"] is None
        assert tg_row["flagged"] is False


class TestRunDiff:
    def test_zero_stored_runs_graceful(self, tmp_memory, capsys):
        rc = bm.run_diff(reports_dir=str(tmp_memory / "benchmark_reports"))
        assert rc == 0
        assert "Not enough" in capsys.readouterr().out

    def test_one_stored_run_graceful(self, tmp_memory, capsys):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-01_000000.json").write_text(
            json.dumps(_fake_report(0.9, 0.8, 0.9, 0.0)), encoding="utf-8"
        )
        rc = bm.run_diff(reports_dir=str(reports_dir))
        assert rc == 0
        assert "Not enough" in capsys.readouterr().out

    def test_two_stored_runs_flags_and_exits_zero_without_strict(self, tmp_memory, capsys):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-01_000000.json").write_text(
            json.dumps(_fake_report(0.90, 0.80, 0.90, 0.0)), encoding="utf-8"
        )
        (reports_dir / "2026-01-02_000000.json").write_text(
            json.dumps(_fake_report(0.50, 0.80, 0.90, 0.0)), encoding="utf-8"
        )
        rc = bm.run_diff(reports_dir=str(reports_dir), strict=False)
        out = capsys.readouterr().out
        assert rc == 0  # informational only without --strict
        assert "MOVED >5pp" in out

    def test_strict_exits_nonzero_when_flagged(self, tmp_memory):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-01_000000.json").write_text(
            json.dumps(_fake_report(0.90, 0.80, 0.90, 0.0)), encoding="utf-8"
        )
        (reports_dir / "2026-01-02_000000.json").write_text(
            json.dumps(_fake_report(0.50, 0.80, 0.90, 0.0)), encoding="utf-8"
        )
        rc = bm.run_diff(reports_dir=str(reports_dir), strict=True)
        assert rc == 2

    def test_strict_exits_zero_when_nothing_flagged(self, tmp_memory):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-01_000000.json").write_text(
            json.dumps(_fake_report(0.90, 0.80, 0.90, 0.0)), encoding="utf-8"
        )
        (reports_dir / "2026-01-02_000000.json").write_text(
            json.dumps(_fake_report(0.91, 0.80, 0.90, 0.0)), encoding="utf-8"
        )
        rc = bm.run_diff(reports_dir=str(reports_dir), strict=True)
        assert rc == 0


class TestRunHistory:
    def test_zero_stored_runs_graceful(self, tmp_memory, capsys):
        rc = bm.run_history(reports_dir=str(tmp_memory / "benchmark_reports"))
        assert rc == 0
        assert "No stored" in capsys.readouterr().out

    def test_one_stored_run_graceful(self, tmp_memory, capsys):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-01_000000.json").write_text(
            json.dumps(_fake_report(0.9, 0.8, 0.9, 0.0)), encoding="utf-8"
        )
        rc = bm.run_history(reports_dir=str(reports_dir))
        assert rc == 0
        out = capsys.readouterr().out
        assert "only 1 stored run" in out

    def test_multiple_runs_table(self, tmp_memory, capsys):
        reports_dir = tmp_memory / "benchmark_reports"
        (reports_dir / "2026-01-01_000000.json").write_text(
            json.dumps(_fake_report(0.9, 0.8, 0.9, 0.0)), encoding="utf-8"
        )
        (reports_dir / "2026-01-02_000000.json").write_text(
            json.dumps(_fake_report(0.8, 0.7, 0.8, 0.0)), encoding="utf-8"
        )
        rc = bm.run_history(reports_dir=str(reports_dir))
        assert rc == 0
        out = capsys.readouterr().out
        assert "2 stored run" in out
        assert "90.0%" in out or "90%" in out


# ---------------------------------------------------------------------------
# P5 -- Operational Cost (Alpha telemetry)
# ---------------------------------------------------------------------------

class TestOperationalCost:
    def test_missing_file_reports_clearly(self, tmp_path):
        result = bm.compute_operational_cost(str(tmp_path / "alpha_telemetry.jsonl"))
        assert result["available"] is False
        assert "No Alpha telemetry found" in result["message"]

    def test_computes_percentiles(self, tmp_path):
        path = tmp_path / "alpha_telemetry.jsonl"
        records = [
            {"latency_ms": lat, "estimated_tokens": tok}
            for lat, tok in [(10, 100), (20, 200), (30, 300), (40, 400), (50, 500)]
        ]
        with open(path, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        result = bm.compute_operational_cost(str(path))
        assert result["available"] is True
        assert result["n"] == 5
        assert result["latency_p50_ms"] == pytest.approx(30.0)
        assert result["latency_p95_ms"] == pytest.approx(48.0)
        assert result["tokens_p50"] == pytest.approx(300.0)

    def test_malformed_lines_skipped_not_crashing(self, tmp_path):
        path = tmp_path / "alpha_telemetry.jsonl"
        with open(path, "w") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"latency_ms": 15, "estimated_tokens": 150}) + "\n")
        result = bm.compute_operational_cost(str(path))
        assert result["available"] is True
        assert result["n_malformed_skipped"] == 1

    def test_empty_file_reports_clearly(self, tmp_path):
        path = tmp_path / "alpha_telemetry.jsonl"
        path.write_text("", encoding="utf-8")
        result = bm.compute_operational_cost(str(path))
        assert result["available"] is False


# ---------------------------------------------------------------------------
# P8 -- Semantic near-duplicates
# ---------------------------------------------------------------------------

class TestSemanticDuplicates:
    @pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")
    def test_missing_index_reports_clearly(self, tmp_path):
        # Only reaches the "index file missing" branch when numpy IS
        # installed (compute_semantic_duplicates checks HAS_NUMPY first --
        # see test_missing_numpy_reports_clearly for that case, which
        # monkeypatches HAS_NUMPY rather than depending on the real install).
        result = bm.compute_semantic_duplicates(str(tmp_path / "index.npz"))
        assert result["available"] is False
        assert "No attention index found" in result["message"]

    def test_missing_numpy_reports_clearly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm, "HAS_NUMPY", False)
        result = bm.compute_semantic_duplicates(str(tmp_path / "index.npz"))
        assert result["available"] is False
        assert "attention" in result["message"]

    @pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")
    def test_finds_near_duplicate_pair_above_threshold(self, tmp_path):
        index_path = tmp_path / "index.npz"
        # lesson_a and lesson_b are identical (cosine 1.0); lesson_c is orthogonal.
        embeddings = np.array([
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ], dtype=np.float32)
        np.savez(
            str(index_path),
            slugs=np.array(["lesson_a", "lesson_b", "lesson_c"]),
            embeddings=embeddings,
            model_name=np.array("x"),
            encoded_field=np.array("x"),
            timestamp=np.array("2026-01-01T00:00:00"),
            n_lessons=np.array(3),
        )
        result = bm.compute_semantic_duplicates(str(index_path))
        assert result["available"] is True
        assert len(result["pairs"]) == 1
        a, b, score = result["pairs"][0]
        assert {a, b} == {"lesson_a", "lesson_b"}
        assert score == pytest.approx(1.0)

    @pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")
    def test_no_pairs_above_threshold(self, tmp_path):
        index_path = tmp_path / "index.npz"
        embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        np.savez(
            str(index_path),
            slugs=np.array(["lesson_a", "lesson_b"]),
            embeddings=embeddings,
            model_name=np.array("x"),
            encoded_field=np.array("x"),
            timestamp=np.array("2026-01-01T00:00:00"),
            n_lessons=np.array(2),
        )
        result = bm.compute_semantic_duplicates(str(index_path))
        assert result["available"] is True
        assert result["pairs"] == []

    @pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")
    def test_never_deletes_or_modifies_lesson_files(self, tmp_path):
        """Recommendation-only: compute_semantic_duplicates must not touch the filesystem
        beyond reading index.npz."""
        index_path = tmp_path / "index.npz"
        embeddings = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
        np.savez(
            str(index_path),
            slugs=np.array(["lesson_a", "lesson_b"]),
            embeddings=embeddings,
            model_name=np.array("x"),
            encoded_field=np.array("x"),
            timestamp=np.array("2026-01-01T00:00:00"),
            n_lessons=np.array(2),
        )
        lesson_file = tmp_path / "lesson_a.md"
        lesson_file.write_text("---\nname: lesson_a\n---\nbody", encoding="utf-8")
        bm.compute_semantic_duplicates(str(index_path))
        assert lesson_file.read_text(encoding="utf-8") == "---\nname: lesson_a\n---\nbody"


# ---------------------------------------------------------------------------
# End-to-end: main() persists by default and doesn't alter existing metrics
# ---------------------------------------------------------------------------

class TestMainPersistsByDefault:
    def _run_main(self, argv):
        """main() only calls sys.exit() explicitly on some paths (--diff/--history,
        the 'no episodes' early return, or --strict with alerts); otherwise it falls off
        the end and returns None without raising. Treat both as a normal return."""
        old_argv = sys.argv
        sys.argv = argv
        try:
            try:
                bm.main()
                return None
            except SystemExit as exc:
                return exc.code
        finally:
            sys.argv = old_argv

    def test_default_invocation_persists_json_report(self, tmp_memory, monkeypatch, capsys):
        write_episode(tmp_memory, "2026-01-01_ep1", proposed=["lesson_a"], validated=["lesson_a"])
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            code = self._run_main(["measure_performance.py"])
        finally:
            bm.BASE_DIR = old_base
        assert code in (0, None)
        stored = glob.glob(os.path.join(str(tmp_memory), "benchmark_reports", "*.json"))
        assert len(stored) == 1
        with open(stored[0]) as fh:
            data = json.load(fh)
        assert data["schema_version"] == bm.SCHEMA_VERSION
        assert "operational_cost" in data
        assert "semantic_duplicates" in data

    def test_no_save_skips_persistence(self, tmp_memory, capsys):
        write_episode(tmp_memory, "2026-01-01_ep1", proposed=["lesson_a"], validated=["lesson_a"])
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            self._run_main(["measure_performance.py", "--no-save"])
        finally:
            bm.BASE_DIR = old_base
        stored = glob.glob(os.path.join(str(tmp_memory), "benchmark_reports", "*.json"))
        assert len(stored) == 0

    def test_strict_flag_causes_nonzero_exit_on_alert(self, tmp_memory):
        # lesson_quality = 0/1 = 0.0, well under the 0.7 default threshold -> alert.
        write_episode(tmp_memory, "2026-01-01_ep1", proposed=["lesson_a"], validated=[])
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            code = self._run_main(["measure_performance.py", "--strict", "--no-save"])
        finally:
            bm.BASE_DIR = old_base
        assert code == 2

    def test_without_strict_alert_is_informational_exit_zero(self, tmp_memory):
        write_episode(tmp_memory, "2026-01-01_ep1", proposed=["lesson_a"], validated=[])
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            code = self._run_main(["measure_performance.py", "--no-save"])
        finally:
            bm.BASE_DIR = old_base
        assert code in (0, None)


class TestExistingMetricsUnchangedByPhase3:
    """Regression guard: none of the Phase 3 additions may change the value of an
    existing metric on the same input data."""

    def test_lesson_quality_formula_unchanged(self):
        episodes = [
            {"lessons_proposed_by_omega": ["a", "b", "c"], "lessons_validated_by_lambda": ["a"]},
        ]
        val, n = bm.compute_lesson_quality(episodes)
        assert val == pytest.approx(1 / 3)

    def test_implicit_retrieval_formula_unchanged(self):
        episodes = [{"lessons_retrieved_by_alpha": ["x", "y"], "lessons_hit": ["x"]}]
        strict, permissive, n = bm.compute_implicit_retrieval(episodes)
        assert strict == pytest.approx(0.5)
        assert permissive == pytest.approx(0.5)

    def test_transfer_gap_formula_unchanged(self, tmp_memory):
        write_lesson(tmp_memory, "lesson_foo", source_episodes=["ep_1"], uses=1)
        episodes = [
            {"name": "ep_2", "project": "proj-a", "lessons_hit": ["lesson_foo"]},
            {"name": "ep_1", "project": "proj-a"},
        ]
        lessons = {"lesson_foo": {"source_episodes": ["ep_1"]}}
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            val, total, untraceable = bm.compute_transfer_gap(episodes, lessons)
        finally:
            bm.BASE_DIR = old_base
        assert val == pytest.approx(0.0)
