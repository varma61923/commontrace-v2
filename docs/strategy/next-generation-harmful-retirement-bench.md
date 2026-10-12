# CodingLessonBench harmful-retirement measurement

The old CodingLessonBench harness measured patch/test outcomes under randomized
lesson exposure but always returned `harmful_retirement_N: null`. It did not
exercise the learner's causal lifecycle, so a negative aggregate effect could
not establish that a harmful lesson was actually retired.

For each task explicitly labeled harmful, one benchmark invocation creates a
temporary CommonTrace store and a `shadow` lesson record pinned to that task's
content revision. A deterministic salt derived from the supplied benchmark seed
and task identity drives the same `holdout_io` assignment log used by production.
Each resolved runner outcome is appended to that task's store, then
`session_learning.reconcile()` performs the existing integrity audit and
sequential causal decision. The benchmark reports N only when the journal's
latest record is actually `retired` with `SOUND` integrity. N counts all
randomized injected and withheld occasions up to that transition. If any labeled
harmful task is unmeasured, remains in shadow/active, or is censored, the
aggregate `harmful_retirement_N` remains null; per-task details preserve the
observed status and count.

Once the journal retires a lesson, later task occasions receive no lesson and
are excluded from randomized-effect estimates. Runner, patch-application,
launch and timeout errors remain unresolved outcomes; they stop further
exposure for that lesson and cannot count as causal failures. The report keeps
execution time and assignment/reconciliation overhead in separate fields.
Effects remain reported separately for each task. Multi-task runs now also use
the fixed-weight task-stratified estimator and simultaneous interval documented
in the [stratified uplift note](next-generation-codinglesson-stratified-uplift.md);
the earlier single-task-only limitation is superseded. This estimator changes
the gate calculation, not the fixture-only evidence or the requirement for a
real SWE model run.
Temporary task stores are isolated by task, and their SQLite handles and
per-root holdout caches are cleared before disk cleanup. This follows
Hindsight's useful per-run sandbox
boundary (`hindsight-system-evals/hindsight_system_evals/coding_agents/sandbox.py:44-82`)
without depending on its backend or copying code.

The tests use deterministic fixture runners to cover harmful retirement,
neutral outcomes, post-retirement withholding, execution-error censoring and
store cleanup. They verify the lifecycle integration; they do not measure a
real model's task success, real-world harmful-retirement N, or extraction cost.

Validation:

```sh
python -m pytest tests/test_coding_lesson_bench.py tests/test_session_learning.py -q
python -m ruff check benchmarks/coding_lesson_bench.py commontrace/holdout_io.py commontrace/memory_journal.py tests/test_coding_lesson_bench.py
```
