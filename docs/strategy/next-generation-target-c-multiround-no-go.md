# Target C: coverage-driven second-pass retrieval (no-go)

**Decision: do not enable the second pass by default.** On a fresh, lexical
seed-0 LongMemEval-S sample, enabling CommonTrace's existing
`Options.second_pass` did not change any of the 100 ranked-turn lists or any
quality metric at a 1,000 estimated-token budget. The paired evidence delta was
0.00 percentage points (95% CI 0.00 to 0.00). On the held-out LoCoMo seed-0
200-question slice, all 200 ranked-turn lists and all quality metrics were
also unchanged. There is no quality gain to justify enabling another retrieval
round, so the default stays off and no product retrieval code changed.

## Measurement

Both runs used CommonTrace's lexical path (`--embedder none --rerank none`),
the same seeded questions, independent fresh SQLite stores, and no model or
answer judge. Token counts are the benchmark's estimated `ceil(characters / 4)`
context size, not tokenizer-exact counts. Evidence coverage is retrieval
evidence, not answer accuracy.

| Dataset and budget | Baseline evidence / complete | Second-pass evidence / complete | Paired evidence delta (95% CI) | Baseline → candidate turn recall@5 | Baseline → candidate p50 / p95 |
|---|---:|---:|---:|---:|---:|
| LongMemEval-S, 100 questions, 1,000 tokens | 74.66% / 63.27% | 74.66% / 63.27% | 0.00 pp [0.00, 0.00], 98 answerable questions | 66.75% → 66.75% | 42.4 / 65.2 ms → 45.8 / 87.3 ms |
| LoCoMo held-out slice, 200 questions, 1,500 tokens | 64.24% / 54.82% | 64.24% / 54.82% | 0.00 pp [0.00, 0.00], 197 scored questions | 44.57% → 44.57% | 12.3 / 18.2 ms → 13.4 / 30.8 ms |

The paired comparison used 2,000 conversation-cluster bootstrap resamples at
95% confidence. The two LongMemEval unanswerable rows were excluded from
evidence and completeness comparisons; three LoCoMo rows lacked resolved gold
evidence. Mean estimated context sizes were unchanged at 970.87 and 1,434.995
tokens, respectively. The benchmark's complete rankings are byte-for-byte
identical by question: 100/100 LongMemEval rows and 200/200 LoCoMo rows. The
single-run latency values are diagnostic and are not controlled latency
acceptance measurements.

The measured LongMemEval artifact is pinned to the official cleaned dataset
SHA-256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
The LoCoMo artifact is pinned to `locomo10.json` SHA-256
`79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`. Both
results use the same CommonTrace product SHA-256
`2f20a1b2e3978b270c90231c52aadaf7a89b96c2281bf5eb6d9b37c48f992afc`.

Read-only Graphify graph inspection located the existing deterministic facet
pass at `commontrace/conversation/search.py` (`subqueries`, `facet_queries`,
`_second_pass`) and EverOS's independent sub-query blocks and core-first
assembly at `src/everos/memory/search/llm_multiround.py` (`search_episodes_llm_multiround`,
`_search_episodes_subq`). The tested candidate enables CommonTrace's existing
coverage-driven pass, which searches terms missed by the first context and
appends newly found turns when budget remains. EverOS's more adaptive,
decider-driven design was not ported. The repository and competitor Graphify
graphs were read only; no `graphify-out` files were regenerated.

## Reproduction

Use fresh store roots for each run. The candidate wrapper only sets
`Options.second_pass=True`; all other flags and retrieval code are identical.
The official data files remain outside the checkout.

```sh
.venv/bin/python -m benchmarks.conversation_bench \
  --dataset longmemeval --data /dev/shm/longmemeval_s_cleaned.json \
  --root /dev/shm/ct-targetc-longmem-baseline-store \
  --budget 1000 --embedder none --rerank none --limit 100 --seed 0 \
  --neighbours 1 --out /dev/shm/ct-targetc-longmem-baseline.json

.venv/bin/python -m benchmarks.target_c_multiround \
  --dataset longmemeval --data /dev/shm/longmemeval_s_cleaned.json \
  --root /dev/shm/ct-targetc-longmem-candidate-store \
  --budget 1000 --embedder none --rerank none --limit 100 --seed 0 \
  --neighbours 1 --compare /dev/shm/ct-targetc-longmem-baseline.json \
  --out /dev/shm/ct-targetc-longmem-candidate.json

.venv/bin/python -m benchmarks.conversation_bench \
  --dataset locomo --data /tmp/locomo10.json \
  --root /dev/shm/ct-targetc-locomo-baseline-store \
  --budget 1500 --embedder none --rerank none --limit 200 --seed 0 \
  --neighbours 1 --out /dev/shm/ct-targetc-locomo-baseline.json

.venv/bin/python -m benchmarks.target_c_multiround \
  --dataset locomo --data /tmp/locomo10.json \
  --root /dev/shm/ct-targetc-locomo-candidate-store \
  --budget 1500 --embedder none --rerank none --limit 200 --seed 0 \
  --neighbours 1 --compare /dev/shm/ct-targetc-locomo-baseline.json \
  --out /dev/shm/ct-targetc-locomo-candidate.json
```

The complete baseline and candidate reports, including per-question rankings,
paired intervals and input/source bindings, are archived here:

- [LongMemEval baseline](measurements/target-c-longmemeval-multiround-baseline-seed0.json)
- [LongMemEval candidate](measurements/target-c-longmemeval-multiround-candidate-seed0.json)
- [LoCoMo baseline](measurements/target-c-locomo-multiround-baseline-seed0-200.json)
- [LoCoMo candidate](measurements/target-c-locomo-multiround-candidate-seed0-200.json)

Each report has an adjacent `.sha256` sidecar. These lexical runs leave the
LongMemEval-S ≥90% target unmet and do not estimate model-judged answer
accuracy. The previously reported dense and cross-encoder candidates still
require their unavailable local model artifacts and a full held-out validation.
