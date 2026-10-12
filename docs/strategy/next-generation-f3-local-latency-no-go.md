# F3 local hybrid latency: no safe query-only optimization

**Decision:** no new retrieval change is justified by the current evidence. The
1M synthetic-adapter acceptance run measures Lance full-hybrid p95 at
200.82 ms and its dense projection at 223.07 ms; FTS5 is 0.592 ms. SQLite's
dense projection is 2,848.97 ms. The query-only candidate already in this
worktree batches canonical hydration and preserved exact ordered output in a
10K diagnostic, but it does not remove the measured dense-index cost. These
reports use a synthetic 4D adapter and do not establish real-model latency or
quality.

Competitor query-planning patterns do not transfer into a safe latency fix:
Graphiti concurrently runs full-text and vector arms before RRF
(`/root/Test/graphiti/graphiti_core/search/search_utils.py:1217-1231`), but
overlapping a sub-millisecond FTS arm cannot remove a 223 ms dense stage.
Hindsight fuses ranked BM25 and vector candidates inside a Postgres query
(`/root/Test/hindsight/hindsight-api-slim/hindsight_api/engine/memory_engine.py:19786-19832`);
CommonTrace's vector projection is separate from canonical facts, so this
would require a new governed projection and freshness contract. EverOS sets
`nprobes` for its Lance IVF search
(`/root/Test/EverOS/src/everos/infra/persistence/backends/lancedb.py:208-229`),
which does not apply to CommonTrace's HNSW-SQ index. Reducing CommonTrace's
`refine_factor` could change dense-only candidates and has no new recall
evidence; the existing refinement sweep selected 4 to meet its measured mean
recall target, with a 0.90 minimum on held-out queries.

Do not change ANN limits, refinement, or canonical admission on this evidence.
The next viable experiment needs a diverse 384D million-vector fixture and a
same-index exact-recall comparison, including scope/lifecycle/evidence rejects,
then the unchanged full-hybrid acceptance harness. The current 1M synthetic
hybrid reports remain the latency baseline; real-model and server latency are
still unmeasured. Detailed prior audits: [query planning](next-generation-f3-query-planning-audit.md),
[competitor comparison](next-generation-f3-competitor-query-planning-no-go.md),
and [Lance handle reuse](next-generation-f3-lance-handle-reuse.md).
