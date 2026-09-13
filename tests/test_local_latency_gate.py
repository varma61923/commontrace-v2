"""commontrace/reference/measure_local_latency.py, run as a command --
specifically that its --max-total-ms/--max-alpha gates actually fail the
process, not just print a warning. This is what .github/workflows/ci.yml's
"perf gate" job depends on to catch a regression back to lesson_cache.py's
O(N)-YAML-reparse-per-query predecessor (measured: 8.1s vs 249ms at 6,400
lessons) automatically, instead of relying on someone re-running the
benchmark by hand and noticing.

Same pattern as tests/test_cross_field_retrieval.py's
TestItRunsAsACommand -- "a gate that always exits 0 looks wired up and
enforces nothing" applies here exactly as it did there.
"""
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Small and fast: this test asserts the GATE MECHANISM works, not the
# product's actual scaling -- that is what --no-cache below exists to
# force a real, reproducible failure without needing a slow, large sweep.
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
        """If this ever passes, the gate has stopped measuring anything --
        not the uncached path having become fast."""
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
        assert uncached.returncode == 0, uncached.stderr  # no gate flag yet -- just measuring
        uncached_worst_ms = max(p["total_ms"] for p in json.loads(uncached.stdout)["points"])

        assert uncached_worst_ms > cached_worst_ms  # the regression is real, not a fluke

        # A ceiling comfortably above the cached path's worst case, and
        # comfortably below the uncached path's, actually distinguishes them.
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
        """Same mechanism, the other gate: an impossibly strict --max-alpha
        must fail, proving the flag is actually wired to the exit code."""
        result = subprocess.run(
            [sys.executable, "-m", "commontrace.reference.measure_local_latency",
             *_FAST_ARGS, "--max-alpha", "0.01"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "alpha" in result.stderr
