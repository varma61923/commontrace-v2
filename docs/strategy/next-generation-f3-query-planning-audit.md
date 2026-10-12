# F3 query-planning audit

This audit keeps the local hybrid p95 gate open. The 1M synthetic-adapter report measured
SQLite search p95 at 2,828.93 ms and Lance at 200.82 ms. Its isolated components attribute
the time to dense projection (SQLite 2,848.97 ms; Lance 223.07 ms); FTS5 candidate retrieval
was 0.436 ms and 0.592 ms. These remain synthetic-adapter results, not real-model latency.
The exact reports and limitations are in [hybrid acceptance](next-generation-hybrid-acceptance.md).

Graphify traced the path from `search_recipes.search()` through `indexed_search()` to
`fts_candidates()`, `fact_vector_projection.search()`, canonical admission, and RRF.
The SQLite vector index performs an exact corpus scan; the Lance path already uses HNSW and
refines candidates. Graphify also shows Graphiti running its full-text and similarity arms
through `semaphore_gather()` before RRF, Hindsight separating storage-backed retrieval
from RRF fusion, and EverOS routing hybrid recall through LanceDB-backed search modules.
Those patterns do not remove the expensive exact SQLite scan or establish an equivalent
owner-filtered CommonTrace projection.

I found no query-planning-only optimization with clear retrieval semantics that can close
the gate. Shrinking the dense candidate cap or Lance refinement can change which eligible
dense-only facts enter RRF, especially when the raw nearest neighbors fail scope, temporal,
authority, or evidence admission. Skipping the dense arm would break the required dense-only
candidate contract. Parallelizing FTS with dense work cannot materially affect the measured
path because FTS takes less than 1 ms while dense retrieval takes 200 ms to 2.8 s.

No retrieval code or tests changed in this audit. The next bounded optimization needs either
a measured ANN path for SQLite or a safe candidate index that filters by the canonical
retrieval context. Before changing candidate caps, refinement, or ANN search settings, test
against the current exact results on a representative diverse 384-dimensional 1M corpus;
report recall@10, scope/lifecycle/evidence rejection behavior, and fresh end-to-end p95 for
each local backend. A full real-model 1M run is still required to claim the gate.
