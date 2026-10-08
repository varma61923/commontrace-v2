# Phase 0: one-command retrieval truth baseline

Run `python -m benchmarks.phase0 --data-dir DATA --output FRESH_OUTPUT` after
installing optional local attention/vendor dependencies and caching their model
artifacts. The official LoCoMo and original LongMemEval-S bytes are SHA-256
checked. The runner selects 154/30 seeded development questions, preserves their
histories and saves their identities as excluded from future confirmation sets.

A fresh output owns its detached original-product worktree. Only benchmark
Python files are overlaid, so the same harness measures the original and current
product source. All attempts save commands, exit statuses, source hashes, logs
and per-case reports. Failed profiles cannot enter paired comparisons. Effective
dense/rerank use is checked, and source-bound matched comparisons produce
conversation-cluster bootstrap intervals and ranked evidence failures.

Conditions: original/current lexical, current dense, original/current dense with
cross-encoder, raw local Mem0 and episodic Graphiti. Estimated 1,000/2,000-unit
budgets retain the product's native packing contract. Exact cl100k_base text
counts and exceedances are reported separately. These counts are not model-native
reader input usage, and no accuracy, power, scale or SOTA claim follows from them.
Vendor-specific packing/configuration is part of each report. Latencies describe
this machine, include first-call preparation, and do not certify target D.

The runner never calls a reader/judge service. With a configured endpoint/model
and an explicit budget, use the underlying conversation harness's `--answer`,
`--answer-model`, `--judge-model`, `--max-cost`, `--max-output-tokens` flags for
judged follow-up. Official LoCoMo token F1/BEAM aggregation remain open gates;
the existing LoCoMo binary grader is clearly a downstream profile.

Validate exact token counts, incomplete/foreign source outputs refused, absent
attention models not misreported, matched source revision and dataset selection,
and end-to-end local reproduction. All package installs remain optional. Rollback
uses an earlier harness with fresh outputs; existing output directories are
refused to protect historical artifacts.
