# Sequential e-value evidence

`commontrace experiment --evidence e-values` exports predictable AIPW betting
evidence and signed per-allocation-era snapshots in `value-evidence.jsonl`.
It does not replace the billing ledger: `experiment.analyze` and `integrity.audit`
remain the evidence of record. Never multiply overlapping snapshots of an era.

The conditional directional null must hold on each occasion, propensities must
be exact, outcomes binary, reporting ignorable, and nuisance predictions based on
preceding outcomes or pre-treatment covariates. Context labels support predictable
stratum adjustment; the Python API also accepts pre-task baseline success rates.
The resulting e-process is finite-sample and safe at a stopping time under those
assumptions. It is not an unrestricted average-effect test under arbitrary drift.

Monitored shared-task decisions use e-BH, which tolerates arbitrary cross-memory
dependence at a joint stopping time. Repeated, separately selected discovery
releases need an online FDR rule. The explicit legacy fixed-horizon diagnostic
still displays BH and is labelled as unsuitable for dependence-robust release
evidence. Historical rows without logged propensities do not receive invented
betting evidence.

For independent deployments or conditionally e-valid eras, multiply e-values.
Under arbitrary dependence, average them. The maximum-so-far wealth is a peeking
diagnostic, not a portable e-value to combine with e-BH.

The seeded regression simulation observes continuous peeking on four memories
sharing the same tasks, compares the BH and e-process rejection rates, and reports
power on planted effects. Removing predictability by allowing the current outcome
to choose its own bet causes that test to fail. Simulations support regression
checks; the guarantees rely on the stated mathematical assumptions.
