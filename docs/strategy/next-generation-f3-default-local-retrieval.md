# F3 default local dense retrieval

The local/team/server profile default is now
`fastembed:BAAI/bge-small-en-v1.5` (384 dimensions). Conversation recall uses
that default when the optional `fastembed` and NumPy dependencies are installed;
multi-channel recall defaults its conversation channel to `auto`, and fact
retrieval selects the same local model unless a caller explicitly disables or
replaces it. Model construction for this default is cache-only. If the extra or
weights are unavailable, the conversation, fact, and indexed-search paths keep
their lexical results. Explicit conversation `--lexical` and fact embedder
`none` still disable dense search.

Conversation `--rerank auto` is cache-only too: a missing cross-encoder leaves
the first-stage ranking intact and reports `explain.rerank_fallback`. An
explicit `--rerank cross-encoder` keeps its online first-use download behavior.
The profile embedding setting also reaches graph similarity through
`COMMONTRACE_GRAPH_EMBEDDER`; graph-provider construction now applies the same
cache-only rule to the canonical default tag. Fact consolidation uses its
separate `COMMONTRACE_CONSOLIDATE_EMBEDDER` opt-in.

Graphify traces CommonTrace's CLI recall path through
`commands/knowledge.py -> recall.py -> conversation.search -> conversation.embed`
and the fact path through `recall.py -> fact_embeddings.py`. The configured
conversation default previously resolved to `arctic-m` only when the attention
extra was installed; direct multi-channel recall passed `embedder="none"`, and
fact recall returned before scoring when no fact embedder was configured.
Conversation recall had no dense-stage cache-miss fallback. Those were the
default-path gaps fixed here.

The installed Cognee checkout uses `BAAI/bge-small-en-v1.5` as its keyless local
embedding default (`cognee/infrastructure/databases/vector/embeddings/config.py`)
and exposes a cache probe in `FastembedEmbeddingEngine.py`. CommonTrace adapts
that model choice through its existing FastEmbed provider, keeps weights
cache-only on the default path, and retains lexical fallback instead of making
FastEmbed a required dependency.

## LoCoMo retrieval check

The FastEmbed extra and official LoCoMo data were installed/downloaded outside
the source tree. The dataset came from the
[Snap Research release](https://github.com/snap-research/locomo), SHA-256
`79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`. The
candidate ran with `COMMONTRACE_OFFLINE=1` and a temporary FastEmbed cache.
The first paired run used a 200-question seed-0 sample across all ten
conversation clusters, a 1,500-token budget, no reranker, and 2,000
clustered-bootstrap draws. A second run evaluated all 1,540 questions with the
same budget and retrieval settings, again using 2,000 paired cluster-bootstrap
draws. Both reports bind the same product, harness, adapter, and dataset hashes.
The full-set commands were:

```sh
.venv/bin/python -m benchmarks.conversation_bench --dataset locomo --data /tmp/commontrace-locomo10.json --root /tmp/commontrace-locomo-f3 --budget 1500 --embedder none --rerank none --limit 0 --seed 0 --bootstrap --out /tmp/commontrace-locomo-lexical-full.json
COMMONTRACE_OFFLINE=1 FASTEMBED_CACHE_PATH=/tmp/commontrace-fastembed-cache .venv/bin/python -m benchmarks.conversation_bench --dataset locomo --data /tmp/commontrace-locomo10.json --root /tmp/commontrace-locomo-f3 --budget 1500 --embedder auto --rerank none --limit 0 --seed 0 --bootstrap --compare /tmp/commontrace-locomo-lexical-full.json --out /tmp/commontrace-locomo-bge-small-full.json
```

The benchmark's paired-comparison bootstrap seed is 42; the dataset sampling
seed is 0 (all questions are included, so there is no sample truncation).

| Metric | Lexical | FastEmbed BGE-small | Paired result |
| --- | ---: | ---: | --- |
| Evidence in context | 64.24% | 68.80% | +4.56 pp; 95% CI −0.13 to +9.07 pp; inconclusive |
| Lexical completeness score | 81.33% | 85.26% | +3.93 pp; 95% CI +1.58 to +6.16 pp |
| Gold answer present in context | 35.76% | 35.15% | −0.61 pp; 95% CI −5.92 to +4.11 pp |
| Recall latency p50 / p95 | 12.3 / 19.1 ms | 25.6 / 67.7 ms | Dense retrieval is slower on this local CPU |

The full-set run is the stronger quality estimate:

| Metric | Lexical | FastEmbed BGE-small | Paired result |
| --- | ---: | ---: | --- |
| Evidence in context | 78.69% | 82.06% | +3.37 pp; 95% CI +1.99 to +4.54 pp |
| All gold evidence IDs found | 72.33% | 75.46% | +3.13 pp; 95% CI +1.99 to +4.23 pp |
| Gold answer present in context | 47.86% | 49.54% | +1.68 pp; 95% CI +0.35 to +2.90 pp |
| Question-term completeness score | 89.73% | 91.20% | +1.47 pp; 95% CI +0.23 to +2.61 pp |
| Recall latency p50 / p95 | 12.4 / 16.6 ms | 26.6 / 37.8 ms | Full-set p95 is below 50 ms |

Three questions with unresolved gold-turn references are excluded from paired
evidence estimates in the 200-question run; four are excluded from the full-set
paired evidence estimate (1,536 questions across ten conversation clusters).
The completeness score and gold-answer-presence metric are retrieval proxies,
not answer correctness. No reader or judge was run, so the LoCoMo answer
accuracy gate remains open. Full-set paired evidence and completeness intervals
are positive, but the 200-question evidence interval includes zero. Latency is
also unresolved: the full-set BGE p95 is 37.8 ms while the 200-question p95 is
67.7 ms, so the <50 ms local target is not established repeatably. The
FastEmbed artifact binds version 0.8.0, model revision
`aa8f8b060edb00e03bfdd08813a2949946c8ba55`, and 384 dimensions.

The source-hashed [200-question lexical report](measurements/target-f3-locomo-lexical-200-seed0.json)
and [FastEmbed report](measurements/target-f3-locomo-fastembed-small-200-seed0.json),
plus the [full-set lexical report](measurements/target-f3-locomo-lexical-full-seed0.json)
and [FastEmbed report](measurements/target-f3-locomo-fastembed-small-full-seed0.json),
include paired comparisons, category details, and provenance. Each has a
neighboring `.sha256` file. The initial fake-provider-only run was superseded
after the benchmark began recording the FastEmbed model revision.
