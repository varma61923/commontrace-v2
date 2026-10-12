# Retrieval quality telemetry

The multi-channel recall span already carried result count, context tokens and
the heuristic evidence-confidence assessment. It did not expose the ranking
score used for the packed result, which made regressions hard to compare per
query.

The span now records the top packed weighted-RRF score as
`recall_top_score_raw` and the gap to its runner-up as
`recall_score_margin_raw`, tagged with the `weighted_rrf` score scale. It also
records the existing assessment confidence and abstention decision. These
scores are raw: they are not probabilities and do not imply that the returned
evidence answered the query. We do not emit `recall_hit`; that label requires
calibrated scores or an explicit outcome. This follows EverOS's useful
separation between calibrated and raw ranking scores
(`EverOS/src/everos/core/observability/tracing/scores.py:40-48,176-219`)
without copying its code.

Only the existing OpenTelemetry span receives these attributes, so external
export remains opt-in (`COMMONTRACE_OTEL=1` or an OTLP endpoint). Attributes are
numeric, boolean or fixed labels; query text, memory text and IDs stay out of
telemetry. Empty recalls report zero raw score and margin plus their existing
abstention state.

Focused validation:

```sh
python -m pytest tests/test_retrieval_quality_telemetry.py tests/test_multichannel_recall.py tests/test_telemetry.py tests/test_telemetry_safety.py -q
python -m ruff check commontrace/recall.py tests/test_retrieval_quality_telemetry.py
```

The tests capture the OpenTelemetry span boundary and verify both a populated
and empty recall. They do not measure judged answer quality or retrieval-score
calibration.

## Paired retrieval regression

The integrated candidate was compared with frozen reference `973e599` using a
seeded, stratified 154-question sample from LoCoMo (10 cases), with embedding
and reranking disabled. At 1,500 and 4,000 tokens, evidence, completeness,
session recall, answer-in-context, token use, and all other reported aggregate
retrieval metrics were identical. No comparison gates failed. Recall p95 was
16.9 / 28.2 ms on the reference and 16.7 / 28.6 ms on the candidate; these
small single-run differences are not a latency claim.

This is a retrieval-only regression. Answer generation and judging were off,
so answer accuracy remains unmeasured. `answer_in_context` reports whether
retrieved evidence supports the expected answer; it is not judged answer
accuracy. The compact retained report includes dataset, harness, product and
raw-report hashes, but no question-level rows:
[retrieval regression report](measurements/retrieval-telemetry-regression.json).

Reproduce the paired gate with:

```sh
env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT \
  /tmp/commontrace-goal-env/bin/python -m benchmarks.compare \
  --baseline /tmp/commontrace-quality-run/reference.json \
  --candidate /tmp/commontrace-quality-run/candidate.json \
  --check --seed 61923
```
