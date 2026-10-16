# Counterfactual diagnostics

`attribution.diagnose` samples an episode and invokes an explicitly supplied
isolated replay runner for approximately 32 randomized memory masks. A fixed
seed, sample probability, per-run cost and total cap are mandatory. Cost is
reserved before execution; a runner must enforce its quoted price and execution
isolation. Digital-twin replay is the default. The built-in coordinate-descent
surrogate exposes signed memory contributions, counterfactual regret and a
poisoning-severity diagnostic, anchored in the transparency log.

`restore_audit` adds an evicted memory only inside the replay and reports whether
restoration merits review. `gate_adjustment` feeds diagnostics into the gate
report, without manufacturing a canonical HELPS/HURTS verdict. Supply separate
step-scoped runners for step attribution. Replay-model error, sparse-surrogate
fit error and robot distribution shift remain limitations; live randomized
outcomes remain the evidence of record.
