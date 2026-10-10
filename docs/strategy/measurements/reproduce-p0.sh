#!/usr/bin/env bash
set -euo pipefail
p0_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$p0_root"
python3 -m benchmarks.next_generation_storage \
  --sizes 1000,10000,30000,1000000 --trials 20 \
  --output "${1:-benchmark-results/p0-storage.json}"
