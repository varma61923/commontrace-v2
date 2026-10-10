#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == "--next-generation" ]]; then
  output="${2:-benchmark-results/next-generation}"
  mkdir -p "$output"
  python3 -m benchmarks.next_generation_storage --sizes 1000,10000,30000,1000000 \
    --trials 20 --legacy-max 30000 --output "$output/storage.json"
  python3 -m benchmarks.injector_bench --model bge-small --repetitions 5 --out "$output/injector.json"
  if command -v node >/dev/null; then
    python3 -m benchmarks.injector_bench --transport-only --repetitions 50 --out "$output/native-transport.json"
  fi
  python3 -m pytest tests/test_memory_journal.py tests/test_session_learning.py \
    tests/test_hook_kit.py tests/test_hook_service.py tests/test_fact_purge.py \
    tests/test_fact_vector_projection.py tests/test_runtime_turn.py tests/test_signed_results.py -q
  python3 -m benchmarks.signed_results --manifest docs/strategy/leaderboard/run.json \
    --verify --trusted-key "$(cat docs/strategy/leaderboard/public-key.txt)"
  echo "Local reports: $output. Reader/judge, BEAM, SWE and server gates require their documented external setup."
  exit 0
fi
output="${1:-benchmark-results/evolution}"
python3 -m benchmarks.evolution --output "$output/first"
python3 -m benchmarks.evolution --output "$output/second"
python3 - "$output" <<'PY'
import json
import sys
from pathlib import Path
from benchmarks.evolution import stable_metrics
root = Path(sys.argv[1])
first = json.loads((root / "first" / "run.json").read_text())
second = json.loads((root / "second" / "run.json").read_text())
if stable_metrics(first) != stable_metrics(second):
    raise SystemExit("Independent processes disagree on functional metrics")
print("Independent processes reproduced identical functional metrics; latency is reported separately.")
print("Report:", root / "second" / "index.html")
PY
