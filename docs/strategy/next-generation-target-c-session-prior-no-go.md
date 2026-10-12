# Target C: session-prior turn reranking (no-go)

**Decision: do not retain this reranking change.** On the same seeded 100-question
LongMemEval-S sample, a first-hit session prior moved evidence coverage from
74.66% to 75.17% at 1,000 tokens (+0.51 pp; paired 95% cluster-bootstrap CI
−1.02 to +2.55 pp). It also lowered turn recall@5 by 3.40 pp (CI −6.80 to
−0.34 pp). The evidence gain is inconclusive and the early-rank regression is
measurable, so there is no product change.

The diagnostic runner adds `alpha / (61 + first_rank_in_session)` to each
candidate's `1 / (61 + global_rank)` and reassembles the context through the
normal budget and safety checks. It uses no gold labels, embeddings, reranker
model, or answer judge. Alpha 0.5 left evidence unchanged; alpha 2.0 produced
the values above. Baseline/candidate means and paired intervals use 2,000
conversation-cluster bootstrap resamples, seed 42. Evidence excludes the two
unanswerable questions; session coverage includes all 100.

## Reproduction

The input was the official cleaned LongMemEval-S JSON at
`/dev/shm/longmemeval_s_cleaned.json`, SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
The 100-question category-stratified seed-0 baseline reproduced 74.66%
evidence coverage and 970.87 mean estimated tokens. The candidate measured
75.17% evidence, 64.29% complete evidence sets, 85.83% session coverage,
971.13 mean tokens, turn recall@5 63.35%, and turn recall@10 75.17%. Baseline
values for these metrics were 74.66%, 63.27%, 85.33%, 970.87, 66.75%, and
75.00%, respectively.

```sh
.venv/bin/python -m benchmarks.target_c_session_prior \
  --session-prior-alpha 0 \
  --dataset longmemeval \
  --data /dev/shm/longmemeval_s_cleaned.json \
  --root /dev/shm/ct-targetc-ranking-baseline \
  --cache-dir /dev/shm/ct-targetc-ranking-cache \
  --budget 1000 --embedder none --rerank none --limit 100 --seed 0 \
  --neighbours 1 --out /dev/shm/ct-targetc-ranking-baseline.json

.venv/bin/python -m benchmarks.target_c_session_prior \
  --session-prior-alpha 2.0 \
  --dataset longmemeval \
  --data /dev/shm/longmemeval_s_cleaned.json \
  --root /dev/shm/ct-targetc-ranking-baseline \
  --cache-dir /dev/shm/ct-targetc-ranking-cache \
  --budget 1000 --embedder none --rerank none --limit 100 --seed 0 \
  --neighbours 1 --out /dev/shm/ct-targetc-ranking-alpha20.json \
  --compare /dev/shm/ct-targetc-ranking-baseline.json
```

These are retrieval-evidence measurements on 100 of 500 questions, not answer
accuracy or a full LongMemEval-S result. The dataset and generated SQLite
stores stay outside the checkout.

## Competitor provenance

The read-only Graphiti snapshot at `964bb11d1e7020e3cc67fab244268b0736b6c219`
uses RRF as a preliminary ordering and offers episode-mention counts and MMR
reranking in `graphiti_core/search/search_utils.py`. Zep's snapshot at
`cfa2ab2bbc3c7085d636479b719bcf64608162cc` exposes RRF, MMR, cross-encoder,
and episode-mention rerankers for graph search in
`integrations/autogen/python/src/zep_autogen/tools.py`. These show practical
ways to combine a relevance ranking with session/episode-level evidence. The
tested session prior is the lowest-cost adaptation of that idea; the paired
LongMemEval result does not justify keeping it. No competitor code was copied.
