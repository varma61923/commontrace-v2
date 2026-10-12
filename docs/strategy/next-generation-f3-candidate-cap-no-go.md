# F3 lower dense candidate cap: no-go

I tested reducing the local dense candidate cap from 200 to 50 for a
10-result hybrid query. The motivation was to reduce canonical hydration and
fusion work. The candidate is unsafe with the current planner because local
ANN results are selected before canonical scope admission.

Graphify traced `search_recipes.indexed_search()` through FTS5 and
`fact_vector_projection.search()`, then `_load_candidate_facts()`, per-result
canonical `admit()` checks, and RRF. The checked Graphiti graph shows
`hybrid_node_search()` scheduling full-text and vector calls with
`semaphore_gather()` before RRF. Hindsight's graph reaches
`search_knowledge_pages()` and `knowledge_bm25_arm()`; its PostgreSQL query
applies bank/tag predicates inside both vector and BM25 arms before SQL RRF.
The Zep graph reaches `search_graph_and_compose_context()`, which parallelizes
edge, node, and episode calls; the separate `legacy` graph node exposes an RRF
helper. These are different storage/filtering contracts, so they do not make a
smaller global CommonTrace ANN result set safe.

## Bounded result

Reproduce with:

```sh
TMPDIR=/dev/shm .venv/bin/python -m benchmarks.f3_candidate_cap_audit
```

The command creates a fresh 10,000-fact SQLite fixture and injects a
deterministic owner-independent vector adapter. Its top 60 hits belong to
`agent:b`; the only `agent:a` hit is rank 61. With context `agent:a`, current
cap 200 returns that target. Candidate cap 50 returns no rows. Both paths
exclude all `agent:b` facts, but exact ordered output parity fails. This
fixture tests planner semantics and scope isolation; it uses neither a real
ANN index nor semantic embeddings, and it makes no latency or recall estimate.

**Decision:** do not lower the global dense cap. A future optimization needs
scope-aware filtering in the vector index (and equivalent lifecycle/evidence
admission) before top-k, followed by parity and recall tests. The archived 1M
full-hybrid p95 remains about 200.82 ms; this diagnostic does not rerun or
change that measurement.
