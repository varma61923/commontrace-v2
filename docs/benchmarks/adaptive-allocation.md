# Adaptive holdout allocation (target E)

`benchmarks/adaptive_allocation_bench.py`, seed 0, 400 runs per cell, 2,000 occasions per run,
alpha 0.05, intervals checked every 50 occasions from 100 on. Source SHA-256 `9a84602db9a228b4…`;
the full output is [`adaptive-allocation.json`](adaptive-allocation.json). This is a seeded simulation of the
estimators and the allocation policy, not a measurement of any deployment.

One memory with a true effect of 0, +5 or +10 points. Each occasion falls in a stratum whose base success rate
is 50% (`flat`), 20/50/80% (`moderate`) or 5/50/95% (`strong`); the memory adds the same effect in every
stratum. "Power" counts a run as detected when its interval first lies above zero at any checkpoint up to
2,000 occasions, which is how an anytime-valid design is used: stop at the first crossing.

| Design | Holdout | Interval |
|---|---|---|
| `fixed10-mixture` | 10% throughout (the shipped default) | beta-mixture per arm, Bonferroni (the shipped anytime interval) |
| `fixed50-mixture` | 50% throughout | the same |
| `fixed50-aipw` | 50% throughout | AIPW scores, asymptotic confidence sequence, running intersection |
| `fixed50-aipw-strata` | 50% throughout | the same, with the stratum in the outcome model |
| `adaptive-aipw-strata` | 50% until proven, then 5% (eras of 250) | the same |

| Strata | Effect | Design | Power by 2,000 [95% CI] | Median occasions to HELPS | Time-uniform coverage | Any-time false positives | Helpful memory withheld |
|---|---|---|---|---|---|---|---|
| flat | +0.00 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| flat | +0.00 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| flat | +0.00 | `fixed50-aipw` | 0.01 [0.01, 0.03] | - | 0.978 | 0.022 | - |
| flat | +0.00 | `fixed50-aipw-strata` | 0.01 [0.00, 0.03] | - | 0.983 | 0.018 | - |
| flat | +0.00 | `adaptive-aipw-strata` | 0.01 [0.00, 0.03] | - | 0.975 | 0.025 | - |
| flat | +0.05 | `fixed10-mixture` | 0.00 [0.00, 0.01] | 1300 | 1.000 | - | 0.10 |
| flat | +0.05 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | - | 0.50 |
| flat | +0.05 | `fixed50-aipw` | 0.31 [0.26, 0.35] | 1250 | 0.983 | - | 0.50 |
| flat | +0.05 | `fixed50-aipw-strata` | 0.30 [0.25, 0.34] | 1200 | 0.988 | - | 0.50 |
| flat | +0.05 | `adaptive-aipw-strata` | 0.33 [0.29, 0.38] | 1200.0 | 0.980 | - | 0.45 |
| flat | +0.10 | `fixed10-mixture` | 0.07 [0.05, 0.10] | 1700 | 1.000 | - | 0.10 |
| flat | +0.10 | `fixed50-mixture` | 0.28 [0.24, 0.33] | 1550 | 1.000 | - | 0.50 |
| flat | +0.10 | `fixed50-aipw` | 0.93 [0.90, 0.95] | 800 | 0.973 | - | 0.50 |
| flat | +0.10 | `fixed50-aipw-strata` | 0.94 [0.92, 0.96] | 850.0 | 0.983 | - | 0.50 |
| flat | +0.10 | `adaptive-aipw-strata` | 0.96 [0.93, 0.97] | 800 | 0.988 | - | 0.29 |
| moderate | +0.00 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| moderate | +0.00 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| moderate | +0.00 | `fixed50-aipw` | 0.01 [0.00, 0.03] | - | 0.985 | 0.015 | - |
| moderate | +0.00 | `fixed50-aipw-strata` | 0.00 [0.00, 0.01] | - | 0.990 | 0.010 | - |
| moderate | +0.00 | `adaptive-aipw-strata` | 0.01 [0.00, 0.02] | - | 0.985 | 0.015 | - |
| moderate | +0.05 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | - | 0.10 |
| moderate | +0.05 | `fixed50-mixture` | 0.01 [0.00, 0.02] | 1275.0 | 1.000 | - | 0.50 |
| moderate | +0.05 | `fixed50-aipw` | 0.39 [0.34, 0.44] | 1125.0 | 0.978 | - | 0.50 |
| moderate | +0.05 | `fixed50-aipw-strata` | 0.45 [0.40, 0.50] | 1300.0 | 0.988 | - | 0.50 |
| moderate | +0.05 | `adaptive-aipw-strata` | 0.46 [0.41, 0.51] | 1375.0 | 0.985 | - | 0.44 |
| moderate | +0.10 | `fixed10-mixture` | 0.07 [0.05, 0.10] | 1300.0 | 1.000 | - | 0.10 |
| moderate | +0.10 | `fixed50-mixture` | 0.28 [0.24, 0.33] | 1550.0 | 1.000 | - | 0.50 |
| moderate | +0.10 | `fixed50-aipw` | 0.94 [0.92, 0.96] | 800.0 | 0.978 | - | 0.50 |
| moderate | +0.10 | `fixed50-aipw-strata` | 1.00 [0.99, 1.00] | 700 | 0.978 | - | 0.50 |
| moderate | +0.10 | `adaptive-aipw-strata` | 0.99 [0.97, 1.00] | 750.0 | 0.968 | - | 0.25 |
| strong | +0.00 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| strong | +0.00 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| strong | +0.00 | `fixed50-aipw` | 0.01 [0.00, 0.02] | - | 0.970 | 0.030 | - |
| strong | +0.00 | `fixed50-aipw-strata` | 0.00 [0.00, 0.01] | - | 0.990 | 0.010 | - |
| strong | +0.00 | `adaptive-aipw-strata` | 0.00 [0.00, 0.01] | - | 0.993 | 0.007 | - |
| strong | +0.05 | `fixed10-mixture` | 0.00 [0.00, 0.01] | 1100 | 1.000 | - | 0.10 |
| strong | +0.05 | `fixed50-mixture` | 0.00 [0.00, 0.01] | 1200 | 1.000 | - | 0.50 |
| strong | +0.05 | `fixed50-aipw` | 0.34 [0.30, 0.39] | 1200 | 0.980 | - | 0.50 |
| strong | +0.05 | `fixed50-aipw-strata` | 0.70 [0.65, 0.74] | 1250 | 0.995 | - | 0.50 |
| strong | +0.05 | `adaptive-aipw-strata` | 0.64 [0.59, 0.69] | 1300.0 | 0.988 | - | 0.41 |
| strong | +0.10 | `fixed10-mixture` | 0.02 [0.01, 0.04] | 1550.0 | 1.000 | - | 0.10 |
| strong | +0.10 | `fixed50-mixture` | 0.12 [0.09, 0.15] | 1700.0 | 1.000 | - | 0.50 |
| strong | +0.10 | `fixed50-aipw` | 0.83 [0.79, 0.87] | 950 | 0.980 | - | 0.50 |
| strong | +0.10 | `fixed50-aipw-strata` | 0.99 [0.98, 1.00] | 750.0 | 0.993 | - | 0.50 |
| strong | +0.10 | `adaptive-aipw-strata` | 1.00 [0.99, 1.00] | 750 | 0.978 | - | 0.26 |

## What this shows

- **Coverage holds.** Across all 45 cells the true effect stayed inside the interval at every checkpoint in at
  least 96.7% of runs, and with no effect at all an interval excluded zero at any checkpoint in at most 3.0% of
  runs. The target asked for coverage of at least 95%: met in every cell, including under adaptive allocation.
- **The new estimator is the big gain.** At +10 points the shipped default (10% holdout, beta-mixture interval)
  detects the memory in 2-7% of runs by 2,000 occasions. Adaptive allocation with the AIPW sequence detects it
  in 96-100%, at a median of 750-800 occasions.
- **Adaptive allocation halves the cost of proving a good memory.** Once proven, a memory drops to a 5% holdout,
  so a +10-point memory is withheld on 25-29% of occasions over the run instead of 50%, with the same power.
- **Target E is not met.** Detecting +5 points with 80% power within 2,000 occasions needs an outcome much more
  predictable than these: the best cell reaches 0.70 [0.65, 0.74] (`strong` strata, fixed 50%), and with no
  predictive stratum it is 0.31-0.33. A fixed-sample test at 50/50 and a 50% baseline already needs about 3,100
  occasions for 80% power; an interval that may be read after every occasion costs more, not less. What closes
  the gap is a stratum label that predicts the outcome well (the `strong` rows), not the allocation rule.

## Reproduce

    python -m benchmarks.adaptive_allocation_bench --reps 400 --out adaptive-allocation.json
