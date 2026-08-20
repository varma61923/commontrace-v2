"""Regression tests for Diagnostics & Benchmark Remediations across M3:
- MEM-01: Standalone root path auto-detection in measure_performance.py and pilot_metrics.py
- MEM-05: CLI argument parsing and forwarding in bench_cmd.py (--diff, --history, --strict, --no-save, --threshold-*)
- BOM decoding in measure_performance.py:parse_frontmatter and data loaders
- Doctor command directory listing error resilience
"""
import argparse
import os
import subprocess
import sys
from unittest.mock import patch

from commontrace.commands import bench_cmd, doctor_cmd
from commontrace.reference import measure_performance as mp
from commontrace.reference import pilot_metrics as pm


# ==============================================================================
# MEM-01: Standalone Root Path Resolution
# ==============================================================================
class TestStandaloneRootPathResolution:
    """MEM-01: Auto-detection of repository root when COMMONTRACE_ROOT is unset."""

    def test_measure_performance_auto_root_points_to_repo_root(self):
        script_dir = os.path.dirname(os.path.abspath(mp.__file__))
        expected_repo = os.path.dirname(os.path.dirname(script_dir))
        assert os.path.abspath(mp._AUTO_ROOT) == os.path.abspath(expected_repo)
        # Check that expected repo has commontrace package and memory
        assert os.path.isdir(os.path.join(mp._AUTO_ROOT, "commontrace"))
        assert os.path.isdir(os.path.join(mp._AUTO_ROOT, "memory"))

    def test_pilot_metrics_root_points_to_repo_root(self, monkeypatch):
        monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
        monkeypatch.delenv("JUSTDOIT_ROOT", raising=False)

        script_path = os.path.abspath(pm.__file__)
        expected_repo = os.path.dirname(os.path.dirname(os.path.dirname(script_path)))
        assert os.path.abspath(pm._ROOT) == os.path.abspath(expected_repo)

    def test_measure_performance_standalone_execution(self):
        """Execute measure_performance.py directly via subprocess without COMMONTRACE_ROOT."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(repo_root, "commontrace", "reference", "measure_performance.py")

        env = dict(os.environ)
        env.pop("COMMONTRACE_ROOT", None)
        env.pop("JUSTDOIT_ROOT", None)

        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            cwd=repo_root,
            env=env,
        )
        assert proc.returncode == 0
        assert "Episodes analyzed" in proc.stdout or "Not enough episodes" in proc.stdout

    def test_pilot_metrics_standalone_execution(self):
        """Execute pilot_metrics.py directly via subprocess without COMMONTRACE_ROOT."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(repo_root, "commontrace", "reference", "pilot_metrics.py")

        env = dict(os.environ)
        env.pop("COMMONTRACE_ROOT", None)
        env.pop("JUSTDOIT_ROOT", None)

        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            cwd=repo_root,
            env=env,
        )
        assert proc.returncode == 0


# ==============================================================================
# MEM-05: Bench CLI Argument Parsing and Forwarding
# ==============================================================================
class TestBenchCliArgumentForwarding:
    """MEM-05: Parse and forward --diff, --history, --strict, --no-save, and --threshold-* flags."""

    def test_bench_parser_accepts_all_remediated_flags(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        bench_cmd.add_parser(subparsers)

        args = parser.parse_args([
            "bench",
            "--diff",
            "--history",
            "--strict",
            "--no-save",
            "--threshold-quality", "0.85",
            "--threshold-retrieval", "0.65",
            "--threshold-never-hit", "0.20",
            "--threshold-unimodal", "0.90",
            "--threshold-semantic", "0.80",
            "--threshold-lexical", "0.75",
            "--threshold-freshness", "0.50",
            "--threshold-composite", "0.70",
        ])

        assert args.diff is True
        assert args.history is True
        assert args.strict is True
        assert args.no_save is True
        assert args.threshold_quality == 0.85
        assert args.threshold_retrieval == 0.65
        assert args.threshold_never_hit == 0.20
        assert args.threshold_unimodal == 0.90
        assert args.threshold_semantic == 0.80
        assert args.threshold_lexical == 0.75
        assert args.threshold_freshness == 0.50
        assert args.threshold_composite == 0.70

    def test_bench_run_forwards_flags_to_measure_performance(self, tmp_path):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        bench_cmd.add_parser(subparsers)

        args = parser.parse_args([
            "bench",
            "--diff",
            "--strict",
            "--no-save",
            "--threshold-quality", "0.85",
            "--threshold-retrieval", "0.60",
            "--threshold-never-hit", "0.25",
            "--threshold-unimodal", "0.95",
            "--threshold-semantic", "0.88",
            "--threshold-lexical", "0.72",
            "--threshold-freshness", "0.45",
            "--threshold-composite", "0.68",
            "--dest", str(tmp_path),
        ])

        with patch("commontrace.commands.bench_cmd.run_script", return_value=0) as mock_run:
            rc = bench_cmd.run(args)
            assert rc == 0
            assert mock_run.called

            call_args = mock_run.call_args[0]
            root_arg, script_arg, extra_arg = call_args[0], call_args[1], call_args[2]

            assert root_arg == str(tmp_path.resolve())
            assert script_arg == "benchmark/measure_performance.py"

            assert "--diff" in extra_arg
            assert "--strict" in extra_arg
            assert "--no-save" in extra_arg
            assert "--threshold-quality=0.85" in extra_arg
            assert "--threshold-retrieval=0.6" in extra_arg
            assert "--threshold-never-hit=0.25" in extra_arg
            assert "--threshold-unimodal=0.95" in extra_arg
            assert "--threshold-semantic=0.88" in extra_arg
            assert "--threshold-lexical=0.72" in extra_arg
            assert "--threshold-freshness=0.45" in extra_arg
            assert "--threshold-composite=0.68" in extra_arg

    def test_bench_run_pilot_mode_forwards_pilot_flags(self, tmp_path):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        bench_cmd.add_parser(subparsers)

        args = parser.parse_args([
            "bench",
            "--pilot",
            "--agent-type", "support",
            "--html",
            "--json",
            "--dest", str(tmp_path),
        ])

        with patch("commontrace.commands.bench_cmd.run_script", return_value=0) as mock_run:
            rc = bench_cmd.run(args)
            assert rc == 0
            assert mock_run.called

            call_args = mock_run.call_args[0]
            root_arg, script_arg, extra_arg = call_args[0], call_args[1], call_args[2]

            assert root_arg == str(tmp_path.resolve())
            assert script_arg == "benchmark/pilot_metrics.py"
            assert "--agent-type" in extra_arg
            assert "support" in extra_arg
            assert "--html" in extra_arg
            assert "--json" in extra_arg


# ==============================================================================
# BOM Decoding in Benchmark & Pilot Data Loaders
# ==============================================================================
class TestBenchBomHandling:
    """UTF-8 BOM handling in measure_performance and pilot_metrics."""

    def test_parse_frontmatter_handles_bom_string(self):
        bom_text = "\ufeff---\nname: bom_lesson\nimportance: 4\n---\nBody text\n"
        fm = mp.parse_frontmatter(bom_text)
        assert isinstance(fm, dict)
        assert fm["name"] == "bom_lesson"
        assert fm["importance"] == 4

    def test_load_episodes_handles_bom_files(self, tmp_path, monkeypatch):
        ep_dir = tmp_path / "memory" / "episodes"
        ep_dir.mkdir(parents=True, exist_ok=True)
        ep_file = ep_dir / "2026-08-20_test_ep.md"

        content = (
            "---\n"
            "name: test_ep\n"
            "project: my_proj\n"
            "verdict: CONFORM\n"
            "importance: 3\n"
            "lessons_retrieved_by_alpha: [lesson_a]\n"
            "lessons_hit: [lesson_a]\n"
            "lessons_proposed_by_omega: [lesson_b]\n"
            "lessons_validated_by_lambda: [lesson_b]\n"
            "---\n"
            "Episode body\n"
        )
        ep_file.write_text(content, encoding="utf-8-sig")

        monkeypatch.setattr(mp, "BASE_DIR", str(tmp_path / "memory"))
        episodes = mp.load_episodes()

        assert len(episodes) == 1
        assert episodes[0]["name"] == "test_ep"
        assert episodes[0]["verdict"] == "CONFORM"

    def test_load_lessons_handles_bom_files(self, tmp_path, monkeypatch):
        l_dir = tmp_path / "memory" / "lessons"
        l_dir.mkdir(parents=True, exist_ok=True)
        l_file = l_dir / "lesson_bom_test.md"

        content = (
            "---\n"
            "name: lesson_bom_test\n"
            "description: test desc\n"
            "domain: testing\n"
            "tags: [test]\n"
            "importance: 4\n"
            "status: active\n"
            "uses: 2\n"
            "---\n"
            "## Rule\nTest rule\n"
        )
        l_file.write_text(content, encoding="utf-8-sig")

        monkeypatch.setattr(mp, "BASE_DIR", str(tmp_path / "memory"))
        lessons = mp.load_lessons()

        assert "lesson_bom_test" in lessons
        assert lessons["lesson_bom_test"]["importance"] == 4
        assert lessons["lesson_bom_test"]["uses"] == 2

    def test_load_traces_handles_bom_files(self, tmp_path):
        t_dir = tmp_path / "memory" / "traces"
        t_dir.mkdir(parents=True, exist_ok=True)
        t_file = t_dir / "trace_bom_test.md"

        content = (
            "---\n"
            "id: trace-bom-123\n"
            "title: Trace with BOM\n"
            "agent_type: code\n"
            "tags: [test]\n"
            "outcome:\n"
            "  resolved: true\n"
            "  repeated_error: false\n"
            "---\n"
            "## Context\nContext\n\n## Solution\nSolution\n"
        )
        t_file.write_text(content, encoding="utf-8-sig")

        traces = pm.load_traces(root=str(tmp_path))
        assert len(traces) == 1
        assert traces[0]["id"] == "trace-bom-123"
        assert traces[0]["outcome"]["resolved"] is True


# ==============================================================================
# Doctor Command Directory Listing Error Resilience
# ==============================================================================
class TestDoctorResilience:
    """Doctor command must tolerate OSError during directory inspection."""

    def test_doctor_tolerates_oserror_on_lessons_dir(self, tmp_path, capsys):
        mem_dir = tmp_path / "memory"
        lessons_dir = mem_dir / "lessons"
        lessons_dir.mkdir(parents=True, exist_ok=True)

        args = argparse.Namespace(dest=str(tmp_path))

        with patch("os.listdir", side_effect=PermissionError("Permission denied")):
            rc = doctor_cmd.run(args)
            assert rc == 0

        captured = capsys.readouterr()
        assert "[commontrace] doctor" in captured.out
        assert "lessons in store" in captured.out
        assert "Done." in captured.out
