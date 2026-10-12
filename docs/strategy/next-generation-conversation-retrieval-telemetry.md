# Direct conversation retrieval telemetry

The direct conversation API previously exported only token/turn counts and a
cache flag. The unified recall path already reported raw weighted-RRF scores
and heuristic abstention. Reuse its existing OpenTelemetry field names and
`Span.set` boundary rather than adding a score sink or exporter.

`conversation.recall` now reports top raw score, raw runner-up margin, score
scale, heuristic coverage confidence, and abstention on both fresh and cached
calls. Its fixed scale is `conversation_weighted_rrf`, with `first_stage` marked
explicitly: this is the conversation-specific fusion score, including existing
entity/time/recency boosts, before reranker ordering. It is not interchangeable
with unified recall's weighted-RRF scale or a reranker's logits.

Only delivered turns with actual ranking scores contribute. Accepted second-pass
turns contribute their facet-query scores; graph-only or profile-only context
does not invent ranking scores. Store just two scalar aggregates in the existing
open `explain` map so cache hits retain the same summary. Selecting the top two
uses constant auxiliary memory. No queries, utterances, session/turn IDs, or
coverage terms enter these fields. The existing caller-supplied `space` span
attribute was removed because it can contain a user identifier.

This follows EverOS's separation of calibrated and raw scores at checkout
`fcd41b57cab6c577211120e166f801038d21b7ae`, in
`src/everos/core/observability/tracing/scores.py` and
`src/everos/memory/search/manager.py`. No competitor code was copied.
No `recall_hit` is emitted: the scores and coverage confidence are uncalibrated.
Existing export opt-in settings and default-off behavior are unchanged.

Tests were written first and initially produced four failures for missing
summary fields. Final focused validation: **9 passed**. This covers fresh/cache
parity, private-field exclusion, caller mutation isolation, blank/empty recall,
unpacked candidates under zero budget, default-off export, accepted second-pass
evidence, and profile-only context. The second-pass fixture changes the raw
margin from `1/61 - 1/62` to zero when a newly delivered facet's top-ranked turn
ties the first pass's top turn. Profile-only context retains coverage confidence
1.0 while reporting zero raw ranking score and margin.

```sh
./.venv/bin/python -m pytest tests/test_retrieval_quality_telemetry.py \
  tests/test_conversation_second_pass.py tests/test_conversation_engine_regressions.py \
  tests/test_conversation_memory.py tests/test_conversation_batch_search.py \
  tests/test_multichannel_recall.py tests/test_telemetry.py tests/test_telemetry_safety.py -q
./.venv/bin/ruff check commontrace/conversation/search.py tests/test_retrieval_quality_telemetry.py
```

Combined result: **139 passed, 15 skipped in 5.85 seconds**; Ruff, compilation,
and diff whitespace checks passed. Skips are optional NumPy-dependent and MCP
runtime tests in this environment. These are telemetry correctness regressions;
no answer-quality, score-calibration, or latency improvement was measured.
