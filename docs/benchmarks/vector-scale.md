# Vector recall at 1M memories (target D)

`benchmarks/vector_scale_bench.py` loads seeded vectors through the product's
`PostgresVectorIndex`, bulk-builds the HNSW index its approximate mode
declares, and times queries through the product `search` call. Exhaustive
search through the same class is the ground truth for recall@10.

Machine: 4-core CPU, 15 GB RAM, Postgres 16 with pgvector 0.8.0 built from
source, shared_buffers 128 MB (the default), on the same machine as the
benchmark client. The vectors are synthetic (unit-normalized points around
2,000 seeded cluster centres with heavy per-dimension noise), so this measures
the index and query path, not an embedding model, and is a hard case for
HNSW: neighbours are only weakly separated. Lexical fusion is not part of this
run.

## 1,000,000 vectors, 384 dimensions, default build (m 16, ef_construction 64)

Load 320 s, index build 167 s, index size 2.0 GB, 200 queries.
Exact search: p50 264 ms, p95 285 ms. Raw output:
[`vector-scale-1m-m16.json`](vector-scale-1m-m16.json).

| ef_search | p50 | p95 | recall@10 | worst query |
| --: | --: | --: | --: | --: |
| 40 | 4.3 ms | 6.6 ms | 0.577 | 0.0 |
| 100 | 5.4 ms | 9.1 ms | 0.772 | 0.0 |
| 200 | 6.9 ms | 13.5 ms | 0.924 | 0.0 |
| 400 | 8.6 ms | 15.2 ms | 0.941 | 0.7 |
| 800 | 20.9 ms | 26.5 ms | 0.945 | 0.7 |

Latency is well inside target D (p50 < 50 ms, p95 < 150 ms) at every
setting. Recall plateaus at 0.945, short of the 0.97 target: with this graph,
searching wider stops helping, which points at build quality rather than
search width. A run with a denser graph (m 24, ef_construction 200) is in
progress and will be added here.

Running this benchmark also found a production bug, fixed in the same change:
after five executions Postgres switched the prepared search to a generic plan
that parsed the query vector once per row and could not use HNSW (1.2 s per
query at only 20,000 vectors). Searches now force a custom plan.

## Reproduce

    python -m benchmarks.vector_scale_bench --dsn postgresql://USER:PASSWORD@localhost/DB \
        --n 1000000 --queries 200 --ef-search 40,100,200,400,800 --out scale.json

The database needs pgvector 0.8 or later and no existing HNSW index for the
dimension (the benchmark refuses to load into one).
