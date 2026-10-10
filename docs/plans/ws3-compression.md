# WS3: Experience compression ladder

**Problem.** Distilled lessons and skills can be worse than the raw trajectories they came from, and nothing checks.

**Design.** Trace -> episode -> observation -> lesson -> skill tiers with a causal promotion gate (`compression.py`, `experience_skills.py`); evidence-linked SFT and DPO exports (`compression.export_preferences`).

**Invariants touched.** A higher tier is promoted only on randomized evidence against the tier below it. Exports carry the evidence ids they were built from.

**Tests.** `tests/test_learning_assurance.py`, `tests/test_skills.py`, `tests/test_dpo_export.py`.

**Benchmark to move.** Lift of promoted skills over raw traces in CausalMemBench-style runs.

**Risks.** Promotion needs outcome volume; small stores stay at the trace tier.

**Rollback.** Promotion is reversible: demoting a skill restores the tier below.
