# WS4: Memory security you can prove

**Problem.** Content screens miss poisoned memories phrased to look benign, laundered through summarizers or split into fragments.

**Design.** Origin-bound authority with Ed25519 receipts (`origin.py`, `memory_authority.py`), least-trusted-source inheritance for derived records, per-action authority policies, PoisonBench and GovBench.

**Invariants touched.** Receipts are verified on every delivery; a record whose receipt fails is never delivered.

**Tests.** `tests/test_poisonbench.py`, `tests/test_govbench.py`, `tests/test_memory_evolution_integration.py`, and `hub/tests/test_row_level_security.py`.

**Benchmark to move.** Target F: 0% attack success at >= 95% clean utility (measured: 0% with an authority policy, 100% clean utility).

**Risks.** A compromised operator or stolen signing key is out of scope; low-stakes actions without a policy still receive external memories by design.

**Rollback.** Authority policies are per action; removing one restores the previous delivery behaviour.
