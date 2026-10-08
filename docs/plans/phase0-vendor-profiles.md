# Phase 0: executable vendor profiles and exact development sampling

Both profiles run their upstream Python library locally over raw conversation
turns. They share canonical source identities, question selection, measurement,
reader/judge code, and estimated context-budget units with CommonTrace. Neither
receives gold answers/evidence. Future history is excluded before vendor indexing.

- `--memory-adapter mem0-raw`: public `Memory.add(infer=False)` and `Memory.search`
  with a local Qdrant collection, MiniLM embeddings, optional native BM25, native
  fusion, threshold zero and 200 candidates. Public batched embeddings memoize
  identical add inputs only; native writes and retrieval remain unchanged.
- `--memory-adapter graphiti-episodic`: public raw EpisodicNode writes, native
  episode BM25 search with RRF and 200 candidates, embedded FalkorDBLite. No
  entity extraction/graph completion or managed Zep API is implied.

Both disable vendor telemetry and close temporary backend resources. Vendor
context assembly retains attributed whole raw turns that fit the budget; its
configuration is reported. Optional dependency/model absence aborts with a nonzero status, never with
CommonTrace or lexical substitute results. The run orchestrator records failed
attempts separately from completed measurements. These
are reproducible configurations, not representations of vendor-best accuracy.

LoCoMo `--limit` now samples exactly that many questions with seeded round-robin
selection across task type and conversation. All histories are retained. Sample
normalization and the complete harness/vendor implementation are source-bound;
old overshooting samples intentionally cannot be paired with new samples.

Validate source/clock/budget/identity boundaries, exact deterministic sampling,
real local vendor ingestion/search/resource cleanup and public-path separation.
Rollback uses the earlier harness with a fresh root, preserving source files and
cached completion storage. Full judged evaluation and official dataset score
profiles remain separate gates.
