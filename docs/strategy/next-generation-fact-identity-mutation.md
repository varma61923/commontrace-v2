# Keep renamed facts under their event identity

The frozen staged-write application suite found that moving a fact to a new ID
inside `mutate_facts()` emitted both the old deletion and the new insertion
using the object's new ID. Both events then claimed version 1 and violated the
event ledger's `(fact_id, version)` uniqueness constraint.

Use the transaction map key as the event identity. When a removed fact was
reinserted under a different ID, build the old ID's removal record from its
detached pre-mutation payload and version. Write the new fact as a separate
initial event, then rebuild projections from the event log. Reject changing
`fact.id` in place while leaving it under the old mapping key.

The new regression failed first on the frozen implementation with
`UNIQUE constraint failed: events.fact_id, events.version`. It now checks both
version histories, the old tombstone payload, FTS and replayed projections.
The regression and the previously failing evidence truncation case pass; the
combined storage, write-contract and evidence-context scope passes 132 tests.
Ruff passes for the changed runtime and regression test.

The full frozen `cd8c1dc` suite completed with 6,748 passed, 126 skipped and
one failure in the same identity-mutation path. This fix is on its descendant
branch; the full suite has not yet run against the fixed source.
