# CodingLessonBench stratified uplift gate

CodingLessonBench now reports a weighted task-average causal effect for multi-task runs while retaining every task-specific effect. This supersedes the earlier no-aggregate-gate note in `next-generation-harmful-retirement-bench.md`. The estimand is the average of each task's injected-minus-withheld test-success rate, using a target task weight fixed before runner execution. By default, weights equal each task's planned share in the runner's round-robin schedule; a supplied `--task-weights weights.json` mapping can name a different positive weight for every task. Weights are normalized and emitted in the result. This is a task-stratified estimate, not a raw pooling of occasion outcomes.

Each task uses CommonTrace's existing beta-binomial mixture confidence interval. Before outcomes are run, the benchmark fixes that task's target horizon to `ceil(planned round-robin slots / 2)`, matching the 50/50 randomized holdout design. This schedule-derived value is passed to the analyzer and replaces the analyzer's default horizon for the benchmark. The analyzer receives `alpha = 0.05 / K` for `K` supplied tasks. Bonferroni gives at least 95% joint coverage across the positive-weight task intervals without assuming independence, and adding their fixed task-weighted bounds covers the declared weighted average. This is a time-uniform mixture-interval guarantee with a horizon chosen from the pre-run schedule; it does not depend on the observed baseline or observed pooled success rate. The interval is available only when every positive-weight stratum has at least the existing minimum of 10 resolved outcomes in each randomized arm.

Any unresolved randomized outcome in a positive-weight stratum blocks the aggregate interval and acceptance gate. The report still preserves each task's resolved estimate, counts, and unresolved status for diagnosis. A missing stratum or insufficient arm count also leaves the gate unevaluated. The existing acceptance threshold is unchanged: the weighted estimate must be at least +5 percentage points and its simultaneous lower confidence bound must be above zero. The benchmark does not infer effects on tasks outside the supplied set or target weights.

This change supplies an estimator and gate, not evidence of real SWE uplift. Fixture tasks validate weighting, heterogeneous task effects, multiplicity adjustment, the fixed preplanned horizon, interval construction, and unresolved-outcome handling only. The production analyzer's AIPW path also receives a pre-run horizon (by default from a fixed 50% baseline), but its coverage remains asymptotic; the CodingLessonBench holdout path uses the exact mixture interval. Task interference, model drift, and external runner failures are not repaired by aggregation.

Focused validation:

```sh
python -m pytest tests/test_coding_lesson_bench.py -q
python -m ruff check benchmarks/coding_lesson_bench.py tests/test_coding_lesson_bench.py
python scripts/dev.py docs --check
```
