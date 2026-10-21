# Sequential e-value evidence

`unimem experiment --evidence e-values` exports predictable AIPW betting
evidence and signed per-allocation-era snapshots in `value-evidence.jsonl`.
It does not replace the billing ledger: `experiment.analyze` and `integrity.audit`
remain the evidence of record. Never multiply overlapping snapshots of an era.

The conditional directional null must hold on each occasion, propensities must
be exact, outcomes binary, reporting ignorable, and nuisance predictions based on
preceding outcomes or pre-treatment covariates. Context labels enter as predictable
per-stratum AIPW arm means. The Python API also accepts a pre-task baseline success
rate as a control variate whose slope is the least-squares slope fitted on
strictly preceding rows (clipped to [-1, 1]); any predictable slope keeps the score
conditionally unbiased, and a well-fitted one reduces its variance.
The resulting e-process is finite-sample and safe at a stopping time under those
assumptions. It is not an unrestricted average-effect test under arbitrary drift.

Monitored shared-task decisions use e-BH, which tolerates arbitrary cross-memory
dependence at a joint stopping time. e-BH governs positive claims (a HELPS verdict,
which bills and releases): a HELPS claim e-BH does not confirm becomes
UNDERPOWERED. A HURTS verdict keeps the marginal anytime interval, because
withdrawing a memory is the conservative action and weakening its trigger would
weaken harm control. Repeated, separately selected discovery releases need an
online FDR rule. The explicit legacy fixed-horizon diagnostic
still displays BH and is labelled as unsuitable for dependence-robust release
evidence. Historical rows without logged propensities do not receive invented
betting evidence. A duplicated (memory, occasion) row in the evidence of record is
dropped (the first row is kept, which never adds evidence) and counted as
`duplicates_dropped`, rather than failing the whole analysis. Hub analyses pass
each row's logged propensity in assignment order.

For independent deployments or conditionally e-valid eras, multiply e-values.
Under arbitrary dependence, average them. The maximum-so-far wealth is a peeking
diagnostic, not a portable e-value to combine with e-BH.

The seeded regression simulation observes continuous peeking on four memories
sharing the same tasks, compares the BH and e-process rejection rates, and reports
power on planted effects. Removing predictability by allowing the current outcome
to choose its own bet causes that test to fail. Simulations support regression
checks; the guarantees rely on the stated mathematical assumptions.
