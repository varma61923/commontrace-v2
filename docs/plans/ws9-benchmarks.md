# WS9: Benchmark leadership

**Problem.** Retrieval benchmarks do not measure whether memory improves outcomes, and published numbers are rarely reproducible.

**Design.** Conversation benchmarks with frozen worktrees and signed manifests (`benchmarks/conversation_bench.py`); CausalMemBench, PoisonBench, GovBench, the adaptive allocation simulation and the robot fleet demo; a paper draft (`docs/research/causal-memory.md`).

**Invariants touched.** Every reported number names its command, seed and source hash. A run whose sources change during measurement is discarded. Ungraded questions are retried, never dropped.

**Tests.** `tests/test_*bench*.py`, `tests/test_grade_rounds.py`.

**Benchmark to move.** Targets A, B and E.

**Risks.** Model-graded accuracy depends on provider quotas; free-tier runs are slow.

**Rollback.** Benchmarks never change product defaults by themselves.
