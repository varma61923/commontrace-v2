# Controlled retrieval quality experiments

All tested context-selection changes were rejected as production defaults.
Several improve one official BEAM conversation, but fail on disjoint
conversations or LoCoMo. The existing configurable semantic arm improves one
measured BEAM 100K conversation at a substantial preparation cost. These are
retrieval diagnostics, not judged answer accuracy or competitor rankings.

The experiment uses detached CommonTrace
`8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`, the official dataset adapters and
source-ID evidence definitions, a fixed 1,500 estimated-token budget, no
reranker, and no generation/judge calls. Competitor-inspired hypotheses are
bounded candidate overfetch, independently selected evidence before neighbors,
session diversification, and preparation of reusable semantic vectors.

## Experimental design

Initial development uses ten category-balanced questions from BEAM 1M
conversation 1, selected with seed 2026. First validation evaluates every
available question from BEAM 100K conversation 2, BEAM 1M conversation 2,
and LoCoMo `conv-30`. BEAM 10M conversation 1 provides another scale check.
All these question-text hash sets are disjoint from the development set and
their first-message hashes differ. Equal numeric conversation IDs in different
BEAM scales do not identify the same transcript. That hash check does not prove
absence of thematic or generation-template relationships between BEAM corpora.

After inspecting those results, a new, explicitly post hoc hypothesis uses a
larger primary evidence quota only for advice or lifetime/habit questions.
It is tested on previously uninspected BEAM 100K conversation 3, BEAM 1M
conversation 3, and LoCoMo `conv-41`. These question sets are also disjoint
from the development questions. Official ability labels, reference answers,
and reference evidence do not influence routing. Gold evidence is read only
after recall for evaluation.

The runner is [retrieval-quality-experiments.py](retrieval-quality-experiments.py).
Input hashes, question hashes, complete option settings, per-category results,
sample denominators, model work, and rejection decisions are recorded in
[retrieval-quality-experiments-results.json](retrieval-quality-experiments-results.json).
The runner additionally writes every per-question result to its specified
output file. The committed summary names those raw experiment artifacts.

## Tested generic variants

* **Baseline:** production defaults, except the official runner's one neighbor
  before/after, explicit lexical mode, and disabled reranker.
* **Direct:** zero neighbors and primary quota twelve
  (`primary_hits=12`, versus the baseline's default three).
* **Direct-wide:** direct selection with candidate pool 1,000 instead of 200.
* **More-primary:** primary quota eight, with the normal neighbor policy.
* **Short-evidence:** excerpt cap 120 estimated tokens and primary quota eight.
* **Diverse:** always enable the existing broad/session-diversified policy.
* **Adaptive-primary:** quota eight when the existing advice detector matches,
  or the question says "have I ever" / "do I usually/typically/generally/normally";
  otherwise retain baseline settings. This rule was fixed before the final
  validation runs, after the first validation had been examined.

## Evidence recall at the same context budget

Values below are percentages. Means exclude questions lacking gold source IDs;
the committed results include that metric's denominator. The baseline samples
are small relative to each benchmark and do not establish full-benchmark scores.

| Variant | BEAM 1M development, 10 questions | BEAM 100K validation, 20 questions | BEAM 1M validation, 20 questions | LoCoMo validation, 81 questions | BEAM 10M transfer, 10 questions |
|---|---:|---:|---:|---:|---:|
| Baseline | 31.25 | 68.61 | 43.12 | 83.42 | 39.16 |
| Direct | 28.13 | 71.39 | 43.33 | 78.44 | Not run |
| Direct-wide | 28.13 | 71.39 | 40.56 | 78.44 | Not run |
| More-primary | 28.13 | 71.39 | 43.12 | 83.42 | 50.27 |
| Short-evidence | 34.38 | 68.66 | 41.61 | 83.42 | 50.27 |
| Diverse | 31.25 | 74.17 | 45.90 | 66.09 | 55.82 |

The apparent best development variant, short-evidence, loses full-evidence
completeness on both held-out BEAM corpora: 55.56→50.00% at 100K and
33.33→27.78% at 1M. Larger pools do not rescue these cases. Forced
diversification improves BEAM but harms LoCoMo single-hop recall
81.82→58.03% and multihop recall 59.70→36.67%. It would sacrifice accurate
narrow recall to obtain a headline result on broader tasks.

More-primary's BEAM 10M improvement is real under this evidence metric, but
it also reduces development knowledge-update evidence from 50→25% on the
TTL question. That illustrates why an overall improvement on one scale is
insufficient justification for replacing the defaults.

## Final disjoint validation of adaptive primary quotas

| Corpus | Questions | Questions activating the quota rule | Baseline evidence recall | Adaptive evidence recall | Baseline full-evidence completeness | Adaptive completeness |
|---|---:|---:|---:|---:|---:|---:|
| BEAM 100K conversation 3 | 20 | 7 | 71.33% | 65.77% | 55.56% | 50.00% |
| BEAM 1M conversation 3 | 20 | 7 | 42.94% | 42.94% | 23.53% | 23.53% |
| LoCoMo `conv-41` | 152 | 1 | 79.25% | 79.25% | 75.00% | 75.00% |

The adaptive hypothesis fails its first independent BEAM test, so it is also
rejected. Unchanged LoCoMo performance alone cannot offset that regression.
Abstention questions lack positive gold evidence and therefore cannot receive
an answerability score from this experiment. Per-category evidence metrics are
useful diagnostics; they do not measure whether the answerer correctly abstains.

## Real local semantic retrieval

An offline, cached `snowflake-arctic-embed-m-v1.5` model prepares one reusable
100K conversation-2 store, then runs the same twenty questions. Its model
weights and raw dataset are not committed. The existing hybrid pipeline uses
the same context budget and no cross-encoder.

| Metric | Lexical baseline | Arctic hybrid |
|---|---:|---:|
| Mean evidence recall | 68.61% | 74.49% |
| Full-evidence completeness | 55.56% | 55.56% |
| Mean estimated context tokens | 1,471.3 | 1,476.8 |
| Passage preparation | None | 896 units, 860 unique texts encoded |
| Actual model encode calls across baseline and excerpt variants | None | 21 |
| Query texts actually encoded across those variants | None | 77 |
| Generation/judge calls | 0 | 0 |

Preparation took 572.15 seconds while sharing CPU with other verification work.
It is an observed cost, not an isolated performance baseline. Twenty query
encoding batches follow the passage encoding call; the second context variant
reuses vectors. A shorter excerpt variant scores 73.98% evidence with unchanged
completeness, so it supplies no reason to replace the existing selection policy.
Embedding input/output tokens are not reported because this encoder does not
return tokenizer usage; text counts are measured instead. Provider API spend is
zero. Local compute cost is not priced.

The semantic result demonstrates a useful optional configuration on one corpus,
not a new generalizing architectural improvement. The large preparation cost
also shows that fast warm vector scans do not eliminate embedding work.

## Reproduction and interpretation

From an isolated checkout at the recorded revision, use an isolated memory root:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
python research/retrieval-quality-experiments.py \
  --checkout "$BASELINE_CHECKOUT" --dataset beam --data "$BEAM_100K_DATA" \
  --conversation-index 2 --root "$EXPERIMENT_ROOT" --out "$RESULT_JSON" \
  --variants baseline,adaptive-primary
```

Use `--dataset locomo --conversation-index 2` for the final LoCoMo test.
For local semantic retrieval, use BEAM 100K conversation index 1 and add
`--embedder arctic-m --prepare --variants baseline,short-evidence`.
The model must already be cached for offline execution. `--read-only` allows
lexical reuse of a prepared source store without modifying it.

Source-ID recall can credit a selected turn even when a short excerpt omits
the exact fact inside that turn. Exact answer-in-context checks are also absent
for most BEAM rubric questions; their small denominator is recorded rather than
presented as benchmark accuracy. Timings share CPU with other tests, variants
run sequentially, and caches warm during a run, so latency differences here are
not accepted speedup measurements. No official prompts, judges, or scores are
edited. The outcome is to retain the proven production defaults and preserve
these failed hypotheses for reproducible future investigation.
