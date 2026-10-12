# F3 canonical candidate hydration

## Change

`indexed_search()` already bounds lexical and dense results, then rechecks
candidate facts against canonical lifecycle, time, scope, authority, and
evidence rules after the vector lookup. That fresh check previously called
`FactMap.get()` once per sparse or dense ID. It now hydrates the deduplicated
candidate union from the same canonical SQLite snapshot in `IN` batches of at
most 500 IDs. Graph-discovered facts still use the existing lazy lookup, and
the admission predicates and RRF ordering are unchanged.

This uses the set-oriented query principle visible in Hindsight's fused
vector/BM25 CTE and final row hydration (`/root/Test/hindsight/hindsight-api-slim/hindsight_api/engine/memory_engine.py:19798-19832`), while keeping CommonTrace's canonical facts authoritative. Hindsight can fuse those arms because both its ranking and bank/tag filters live in Postgres. CommonTrace's dense projection is separate and can be stale relative to canonical events, so the bounded ID load happens only after ANN returns and its canonical checks still run. Graphiti's concurrent FTS/vector calls (`/root/Test/graphiti/graphiti_core/search/search_utils.py:1220-1231`) cannot materially reduce this local path: measured FTS p95 is below 1 ms. EverOS tunes `nprobes` on its Lance dense search (`/root/Test/EverOS/src/everos/infra/persistence/backends/lancedb.py:218-229`); that setting does not transfer to CommonTrace's HNSW-SQ index.

## Controlled measurement

I ran a same-working-source ABBA diagnostic against a 10,000-fact canonical
fixture. Each case included FTS candidate retrieval, canonical admission, graph
seed expansion, RRF, and recipe ordering; it injected 200 deterministic dense
candidate IDs to isolate this planner stage from embedding and ANN latency. The
query used a fixed `as_of` and `context=["agent:a"]`. The scalar case replaced
only the loader with the previous `canonical.get(id)` loop. Forty samples per
case alternated scalar, batch, batch, scalar. Ordered IDs, scores, and signal
maps matched exactly.

| Hydration path | p50 | p95 |
| --- | ---: | ---: |
| One canonical `get()` per ID | 15.011 ms | 21.263 ms |
| Batched ID hydration | 13.893 ms | 15.239 ms |

The p50 fell 7.4%. The p95 reduction is a small-run diagnostic and remains
sensitive to runtime noise. Reproduce it with the installed project venv:

```sh
.venv/bin/python - <<'PY'
import math, statistics, tempfile, time
from benchmarks.hybrid_acceptance import _seed_canonical_fixture
from commontrace import fact_store, search_recipes

root = tempfile.mkdtemp(prefix="commontrace-f3-candidate-", dir="/dev/shm")
_seed_canonical_fixture(root, 10_000)
with fact_store.transaction(root) as facts:
    ids = [row[0] for row in facts.connection.execute(
        "SELECT id FROM facts ORDER BY rowid LIMIT 200")]
scores = {identity: 0.9 - (index % 7) * 0.01 for index, identity in enumerate(ids)}
batch = search_recipes._load_candidate_facts
def scalar(canonical, identities):
    return {identity: canonical.get(identity) for identity in dict.fromkeys(identities)}
samples, expected = {"batch": [], "scalar": []}, None
for mode in ["scalar", "batch", "batch", "scalar"] * 20:
    search_recipes._load_candidate_facts = batch if mode == "batch" else scalar
    started = time.perf_counter_ns()
    rows = search_recipes.search(root, "runtime policy", dense_scores=scores,
        embedder="none", reranker="none", limit=10, context=["agent:a"],
        as_of="2030-01-01T00:00:00Z")
    samples[mode].append((time.perf_counter_ns() - started) / 1_000_000)
    result = [(row["id"], row["score"], row["signals"]) for row in rows]
    if expected is None:
        expected = result
    assert result == expected
search_recipes._load_candidate_facts = batch
for mode, values in samples.items():
    ordered = sorted(values)
    print(mode, "n=40", "p50_ms=", round(statistics.median(values), 3),
          "p95_ms=", round(ordered[math.ceil(0.95 * len(values)) - 1], 3))
print("exact ordered output parity; facts=10000; dense_candidate_ids=", len(ids))
PY
```

The production acceptance harness also passed its dense-only, lexical-only,
and out-of-scope checks on a 10,000-fact SQLite synthetic fixture:

```sh
.venv/bin/python -m benchmarks.hybrid_acceptance --facts 10000 --trials 20 \
  --synthetic-adapter --seed-mode canonical_fixture \
  --output /dev/shm/commontrace-f3-hybrid-batched-sqlite-10k.json
```

It measured full-query p95 35.280 ms, but the harness marks the gate unverified
because the acceptance target requires 1,000,000 facts. This run is SQLite and
uses a synthetic embedding adapter. A fresh Lance acceptance attempt could not
run in this venv because the optional `lancedb` package is unavailable.

## F3 status

This optimization reduces local candidate-planning overhead; it does not close
the local hybrid latency gate. The previously measured 1,000,000-fact Lance
synthetic run remains a miss: full hybrid p95 was 200.822 ms, dense projection
p95 was 223.066 ms, and FTS p95 was 0.592 ms. The exact report is
[`f1-hybrid-1m-synthetic-lance.json`](measurements/f1-hybrid-1m-synthetic-lance.json).
These synthetic-adapter results are plumbing measurements, not real-model
latency or relevance evidence. The full Lance projection miss remains open.

Targeted validation:

```sh
.venv/bin/pytest -q tests/test_indexed_search.py tests/test_hybrid_graph_seeds.py \
  tests/test_retrieval_policy_vectors.py tests/test_fact_vector_projection.py
.venv/bin/ruff check commontrace/search_recipes.py tests/test_indexed_search.py
```
