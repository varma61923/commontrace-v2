# WS2: Causal engine 2.0

**Problem.** A fixed holdout rate is either slow (10%) or expensive (50%), correlational signals withdraw helpful memories, and operators look at results continuously.

**Design.** Randomized per-memory holdout with anytime-valid verdicts (`experiment.py`); exploration slots and off-policy evaluation (`causal_policy.py`, `policy.py`); heterogeneous and interaction effects (`heterogeneity.py`, `interactions.py`); adaptive allocation in hash-chained eras read by AIPW with an asymptotic confidence sequence (`allocation.py`, `commontrace allocate`).

**Invariants touched.** PROTOCOL 13.1 assignment is unchanged; schedules only choose the rate (13.5). Every assignment logs its rate. A schedule is computed from earlier outcomes only and takes effect after it is written. Unscheduled mid-run rate changes still invalidate.

**Tests.** `tests/test_experiment*.py`, `tests/test_integrity.py`, `tests/test_allocation.py` (including a signed proof round trip and tamper detection), `tests/test_conformance.py` (40 allocation vectors).

**Benchmark to move.** Target E. The fixed-prior run in `docs/benchmarks/adaptive-allocation.md` has empirical time-uniform coverage >= 96.75% in every cell; the AIPW sequence remains asymptotic. +10pp adaptive power is 0.96-1.00; +5pp is not reached (best strong-strata power 0.70).

**Risks.** The AIPW sequence's coverage is asymptotic; it claims nothing before 50 scores and is measured from 100 occasions on. Low monitoring rates make scores noisy; the running intersection keeps verdicts from flipping.

**Rollback.** `commontrace allocate disable` returns to the fixed rate from the next assignment; lessons assigned under schedules stay estimated by AIPW.
