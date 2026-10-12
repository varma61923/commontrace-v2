# Target C: LongMemEval-S retrieval evidence

**Decision: no ranking change is retained; the >=90% evidence target remains
open.** The strongest existing result in the scorecard is 86.56% on a seeded
100-question LongMemEval-S subset with cross-encoder blend 3. It is historical
and was not reproduced in this pass. The held-out LoCoMo check previously
rejected blend 3 as a default.

## Canonical data and verified rerun

The cleaned LongMemEval-S artifact is available from the [official LongMemEval
repository](https://github.com/xiaowu0162/longmemeval), which links the cleaned
release on [Hugging Face](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned).
It was missing from this checkout, so I fetched `longmemeval_s_cleaned.json` to
`/dev/shm` without adding the 277 MB dataset to the repository. Its SHA-256
matches the pinned digest in `docs/performance.md` exactly:
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.

The available `/tmp/longmemeval_s.json` file is a different Graphiti
`longmemeval_oracle.json` copy (`821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`).
It contains only evidence sessions, not the full distractor history; two
exploratory runs against it were excluded from all results.

Using the official cleaned artifact, a seeded 100-question lexical run at a
1,000-token budget measured **74.66% evidence coverage** and **970.87 estimated
tokens**. This reproduces the scorecard's lexical result on the same sample.
The source-bound output and checksummed dataset identity are retained in the
[measurement report](measurements/target-c-longmemeval-official-lexical-seed0.json).
The run overlapped a separate local setup smoke, so its latency fields are
diagnostic only and are not used for acceptance.

The local `.venv` lacks `sentence_transformers`, `torch`, and `transformers`,
so the dense-plus-cross-encoder result could not be reproduced. No weighting or
default change is justified without the pinned full LongMemEval-S run, the
required model artifacts, and a held-out LoCoMo check. Evidence coverage is a
retrieval metric, not answer accuracy.

## Retrieval audit

Graphiti at commit `964bb11d1e7020e3cc67fab244268b0736b6c219` uses BM25 and
cosine search arms with reciprocal-rank fusion, with cross-encoder reranking as
an option. CommonTrace already combines lexical and dense RRF rankings before
cross-encoder reranking in `commontrace/conversation/search.py`. This pattern
does not identify a safe new port that can be validated without the optional
model artifacts and full target evaluation.

## Reproduction

```sh
curl -fL --output /dev/shm/longmemeval_s_cleaned.json \
  'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json'
sha256sum /dev/shm/longmemeval_s_cleaned.json
.venv/bin/python -m benchmarks.conversation_bench \
  --dataset longmemeval \
  --data /dev/shm/longmemeval_s_cleaned.json \
  --root /dev/shm/commontrace-longmemeval-correct \
  --cache-dir /dev/shm/commontrace-longmemeval-cache \
  --budget 1000 --embedder none --rerank none --limit 100 --seed 0 \
  --out /dev/shm/target-c-longmemeval-official-lexical-seed0.json
```

The checked-in report has an adjacent SHA-256 sidecar. The raw benchmark
dataset and temporary SQLite store remain outside the checkout.
