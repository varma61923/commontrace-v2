# WS5: Federated commons and network effects

**Problem.** A lesson proven in one fleet is worth more if others can adopt it safely, and publishers need to be paid for value they create.

**Design.** Federation with randomized response (`federation.py`); a lesson marketplace where fleets sign measured lift, a referee pools independent organisations, and publishers sign listings (`marketplace.py`, Hub `market.py`); settlement with an outcome share paid only on the buyer's own HELPS verdict (`market_settlement.py`).

**Invariants touched.** Installed lessons land at status=review. Lift certificates bind the exact lesson text. Hub market tables are protected by row-level security with read-only public policies.

**Tests.** `tests/test_marketplace.py`, `hub/tests/test_market.py`, `hub/tests/test_row_level_security.py`.

**Benchmark to move.** Adoption and replicated lift across organisations (not measurable offline).

**Risks.** Privacy guarantees of the federated statistics are experimental and stated as such.

**Rollback.** Listings can be withdrawn; installs are ordinary review drafts that can be rejected.
