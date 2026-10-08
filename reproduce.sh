#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
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
