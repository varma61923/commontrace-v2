import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FAST_ARGS = ["--sizes", "50,200", "--runs", "2"]


class TestItRunsAsACommand:
    def test_emits_parseable_json(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "commontrace.reference.measure_local_latency",
             *_FAST_ARGS, "--json"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["points"]
        assert payload["use_cache"] is True

    def test_passes_with_a_generous_ceiling(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "commontrace.reference.measure_local_latency",
             *_FAST_ARGS, "--max-total-ms", "5000"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestTheGateActuallyCatchesTheRegressionItExistsFor:
    def test_the_uncached_path_fails_a_ceiling_the_cached_path_clears(self, tmp_path):
        cached = subprocess.run(
            [sys.executable, "-m", "commontrace.reference.measure_local_latency",
             "--sizes", "100,1600", "--runs", "2", "--json"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert cached.returncode == 0, cached.stderr
        cached_worst_ms = max(p["total_ms"] for p in json.loads(cached.stdout)["points"])

        uncached = subprocess.run(
            [sys.executable, "-m", "commontrace.reference.measure_local_latency",
             "--sizes", "100,1600", "--runs", "2", "--no-cache", "--json"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert uncached.returncode == 0, uncached.stderr
        uncached_worst_ms = max(p["total_ms"] for p in json.loads(uncached.stdout)["points"])

        assert uncached_worst_ms > cached_worst_ms

        ceiling = (cached_worst_ms + uncached_worst_ms) / 2
        gated = subprocess.run(
            [sys.executable, "-m", "commontrace.reference.measure_local_latency",
             "--sizes", "100,1600", "--runs", "2", "--no-cache",
             "--max-total-ms", str(ceiling)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert gated.returncode == 1
        assert "FAIL" in gated.stderr
        assert "exceeds" in gated.stderr

    def test_max_alpha_flag_fails_the_process_when_exceeded(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "commontrace.reference.measure_local_latency",
             *_FAST_ARGS, "--max-alpha", "0.01"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "alpha" in result.stderr
