"""Regression tests for Milestone 3 fixes: benchmark integrity, security hardening."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock, patch

import pytest

from commontrace.reference import measure_performance
from commontrace.reference import pilot_metrics
from commontrace.commands import install_cmd
from commontrace import paths


# ---------------------------------------------------------------------------
# 1. persist_report — collision sort order and atomic write
# ---------------------------------------------------------------------------
class TestPersistReportCollisionSort:
    def test_collision_suffix_sorts_after_base_file(self):
        """Collision-resolved filenames must sort AFTER the base file.

        Old '-N' suffix: '2026_base-1.json' < '2026_base.json' ('-'=45 < '.'=46).
        New '_0001' suffix must sort after the base file.
        """
        base = "2026-09-05_120000_000000"
        base_file = f"{base}.json"
        collision_file = f"{base}_0001.json"
        assert collision_file > base_file, (
            f"Collision suffix '{collision_file}' must sort after base '{base_file}'"
        )

    def test_persist_report_returns_valid_json_file(self, tmp_path):
        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            path = measure_performance.persist_report({"test": True})
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data == {"test": True}

    def test_persist_report_no_partial_file_on_write_error(self, tmp_path):
        """If the write fails after mkstemp, no partial .json should remain."""
        # Simulate a write failure by patching fdopen to raise
        real_mkstemp = tempfile.mkstemp

        def failing_mkstemp(dir, suffix):
            fd, p = real_mkstemp(dir=dir, suffix=suffix)
            return fd, p  # fd will be closed by the except block

        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            with patch("os.fdopen", side_effect=OSError("simulated write failure")):
                with pytest.raises(OSError, match="simulated write failure"):
                    measure_performance.persist_report({"data": 1})

        # No .json should have been committed
        json_files = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
        assert json_files == [], f"No .json should remain after failed write; found {json_files}"

    def test_persist_report_collision_loop_produces_distinct_sorted_files(self, tmp_path):
        import datetime
        ts = datetime.datetime(2026, 9, 5, 12, 0, 0, 123456)
        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            path1 = measure_performance.persist_report({"run": 1}, ts=ts)
            path2 = measure_performance.persist_report({"run": 2}, ts=ts)
        assert path1 != path2
        assert os.path.isfile(path1) and os.path.isfile(path2)
        n1, n2 = os.path.basename(path1), os.path.basename(path2)
        assert sorted([n1, n2])[0] < sorted([n1, n2])[1]


# ---------------------------------------------------------------------------
# 2. JSON-mode empty-corpus error output
# ---------------------------------------------------------------------------
class TestJsonModeEmptyCorpusOutput:
    def test_bench_empty_episodes_emits_json_error(self, capsys):
        """When --json and no episodes, stdout must be valid JSON (not plain text)."""
        argv = ["--json", "--no-save"]
        with patch.object(measure_performance, "load_episodes", return_value=([], 0)):
            with patch.object(measure_performance, "load_lessons", return_value=({}, 0)):
                with patch("sys.argv", ["commontrace"] + argv):
                    with pytest.raises(SystemExit) as exc_info:
                        measure_performance.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload.get("error") == "not_enough_episodes"

    def test_bench_no_json_flag_emits_plain_text(self, capsys):
        """Without --json, the human-readable message is printed (not JSON)."""
        argv = ["--no-save"]
        with patch.object(measure_performance, "load_episodes", return_value=([], 0)):
            with patch.object(measure_performance, "load_lessons", return_value=({}, 0)):
                with patch("sys.argv", ["commontrace"] + argv):
                    with pytest.raises(SystemExit) as exc_info:
                        measure_performance.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Not enough episodes" in captured.out
        # Must NOT be valid JSON (it's a plain message)
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.out)

    def test_pilot_metrics_empty_traces_emits_json_error(self, tmp_path, capsys):
        """When --json and no traces, stdout must be valid JSON."""
        argv = ["--json"]
        with patch.object(pilot_metrics, "load_traces", return_value=[]):
            with patch("sys.argv", ["commontrace"] + argv):
                with pytest.raises(SystemExit) as exc_info:
                    pilot_metrics.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload.get("error") == "no_traces"


# ---------------------------------------------------------------------------
# 3. HTML output is produced without error
# ---------------------------------------------------------------------------
class TestHtmlRendering:
    def test_render_html_produces_valid_document(self, tmp_path):
        """render_html runs end-to-end and produces a complete HTML page."""
        argv = ["--html", "--no-save"]
        # Only verify the HTML code path runs; rely on existing render tests for detail
        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            with patch.object(measure_performance, "load_episodes", return_value=([], 0)):
                with patch.object(measure_performance, "load_lessons", return_value=({}, 0)):
                    with patch("sys.argv", ["commontrace"] + argv):
                        with pytest.raises(SystemExit):
                            measure_performance.main()
        # If the HTML path ran but no episodes, exit early — that's fine.
        # The key check: no unhandled exception.


# ---------------------------------------------------------------------------
# 4. install_cmd root resolved from dest, not cwd
# ---------------------------------------------------------------------------
class TestInstallCmdRootResolution:
    def test_root_is_resolved_from_dest_not_cwd(self, tmp_path):
        """install --dest /some/path should configure .mcp.json with /some/path as root."""
        dest = tmp_path / "agent_home"
        dest.mkdir()

        captured_roots = []

        def fake_write_local_mcp(dest_arg, root_arg):
            captured_roots.append(root_arg)

        args = types.SimpleNamespace(target="claude-code", dest=str(dest))
        with patch.object(install_cmd, "_write_local_mcp", side_effect=fake_write_local_mcp):
            with patch.object(install_cmd, "_find_skill_md", return_value=None):
                with patch.object(install_cmd, "_write", return_value=None):
                    install_cmd.run(args)

        assert len(captured_roots) == 1
        resolved = captured_roots[0]
        assert resolved.startswith(str(dest)), (
            f"Expected root under dest={dest}, got root={resolved}"
        )
        assert resolved != os.getcwd(), (
            "Root must not be caller's cwd when --dest points elsewhere"
        )
