# Adaptive holdout allocation (target E)

`benchmarks/adaptive_allocation_bench.py`, seed 0, 400 runs per cell, 2,000 occasions per run,
alpha 0.05, intervals checked every 50 occasions from 100 on. Source SHA-256 `6c0565fd94dc56222a6e6e53fcb8f6d6d69b92f3f55c2618eb81a452afea4007`;
the full output is [`adaptive-allocation.json`](adaptive-allocation.json). This is a seeded simulation of the estimators and allocation policy, not a measurement of any deployment. The beta-mixture interval uses a predeclared horizon of `target_n_per_arm=99` (a +20-point target effect at a 50% baseline), never an observed baseline. AIPW confidence-sequence coverage is asymptotic under its assumptions; this simulation measures calibration, not a finite-sample guarantee.

One memory with a true effect of 0, +5 or +10 points. Each occasion falls in a stratum whose base success rate
is 50% (`flat`), 20/50/80% (`moderate`) or 5/50/95% (`strong`); the memory adds the same effect in every
stratum. "Power" counts a run as detected when its interval first lies above zero at any checkpoint up to
2,000 occasions, which is how a sequential design is used: stop at the first crossing. The mixture interval is anytime-valid; AIPW results below are empirical because that sequence is asymptotic.

| Design | Holdout | Interval |
|---|---|---|
| `fixed10-mixture` | 10% throughout (the shipped default) | beta-mixture per arm, Bonferroni (the shipped anytime interval) |
| `fixed50-mixture` | 50% throughout | the same |
| `fixed50-aipw` | 50% throughout | AIPW scores, asymptotic confidence sequence, running intersection |
| `fixed50-aipw-strata` | 50% throughout | the same, with the stratum in the outcome model |
| `adaptive-aipw-strata` | 50% until proven, then 5% (eras of 250) | the same |

| Strata | Effect | Design | Power by 2,000 [95% CI] | Median occasions to HELPS | Empirical time-uniform coverage | Any-time false positives | Helpful memory withheld |
|---|---:|---|---:|---:|---:|---:|---:|
| flat | +0.00 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| flat | +0.00 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| flat | +0.00 | `fixed50-aipw` | 0.01 [0.01, 0.03] | 900 | 0.978 | 0.022 | - |
| flat | +0.00 | `fixed50-aipw-strata` | 0.01 [0.00, 0.03] | 575 | 0.983 | 0.018 | - |
| flat | +0.00 | `adaptive-aipw-strata` | 0.01 [0.00, 0.03] | 850 | 0.975 | 0.025 | - |
| flat | +0.05 | `fixed10-mixture` | 0.00 [0.00, 0.01] | 1300 | 1.000 | - | 0.10 |
| flat | +0.05 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | - | 0.50 |
| flat | +0.05 | `fixed50-aipw` | 0.31 [0.26, 0.35] | 1250 | 0.983 | - | 0.50 |
| flat | +0.05 | `fixed50-aipw-strata` | 0.30 [0.25, 0.34] | 1200 | 0.988 | - | 0.50 |
| flat | +0.05 | `adaptive-aipw-strata` | 0.33 [0.29, 0.38] | 1200 | 0.980 | - | 0.45 |
| flat | +0.10 | `fixed10-mixture` | 0.07 [0.05, 0.10] | 1700 | 1.000 | - | 0.10 |
| flat | +0.10 | `fixed50-mixture` | 0.28 [0.24, 0.33] | 1550 | 1.000 | - | 0.50 |
| flat | +0.10 | `fixed50-aipw` | 0.93 [0.90, 0.95] | 800 | 0.973 | - | 0.50 |
| flat | +0.10 | `fixed50-aipw-strata` | 0.94 [0.92, 0.96] | 850 | 0.983 | - | 0.50 |
| flat | +0.10 | `adaptive-aipw-strata` | 0.96 [0.93, 0.97] | 800 | 0.988 | - | 0.29 |
| moderate | +0.00 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| moderate | +0.00 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| moderate | +0.00 | `fixed50-aipw` | 0.01 [0.00, 0.03] | 550 | 0.985 | 0.015 | - |
| moderate | +0.00 | `fixed50-aipw-strata` | 0.00 [0.00, 0.01] | 1200 | 0.990 | 0.010 | - |
| moderate | +0.00 | `adaptive-aipw-strata` | 0.01 [0.00, 0.02] | 1000 | 0.985 | 0.015 | - |
| moderate | +0.05 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | - | 0.10 |
| moderate | +0.05 | `fixed50-mixture` | 0.01 [0.00, 0.02] | 1275 | 1.000 | - | 0.50 |
| moderate | +0.05 | `fixed50-aipw` | 0.39 [0.34, 0.44] | 1125 | 0.978 | - | 0.50 |
| moderate | +0.05 | `fixed50-aipw-strata` | 0.45 [0.40, 0.50] | 1300 | 0.988 | - | 0.50 |
| moderate | +0.05 | `adaptive-aipw-strata` | 0.46 [0.41, 0.51] | 1375 | 0.985 | - | 0.44 |
| moderate | +0.10 | `fixed10-mixture` | 0.07 [0.05, 0.10] | 1300 | 1.000 | - | 0.10 |
| moderate | +0.10 | `fixed50-mixture` | 0.28 [0.24, 0.33] | 1550 | 1.000 | - | 0.50 |
| moderate | +0.10 | `fixed50-aipw` | 0.94 [0.92, 0.96] | 800 | 0.978 | - | 0.50 |
| moderate | +0.10 | `fixed50-aipw-strata` | 1.00 [0.99, 1.00] | 700 | 0.978 | - | 0.50 |
| moderate | +0.10 | `adaptive-aipw-strata` | 0.99 [0.97, 1.00] | 750 | 0.968 | - | 0.25 |
| strong | +0.00 | `fixed10-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| strong | +0.00 | `fixed50-mixture` | 0.00 [0.00, 0.01] | - | 1.000 | 0.000 | - |
| strong | +0.00 | `fixed50-aipw` | 0.01 [0.00, 0.02] | 325 | 0.970 | 0.030 | - |
| strong | +0.00 | `fixed50-aipw-strata` | 0.00 [0.00, 0.01] | 850 | 0.990 | 0.010 | - |
| strong | +0.00 | `adaptive-aipw-strata` | 0.00 [0.00, 0.01] | 1900 | 0.993 | 0.007 | - |
| strong | +0.05 | `fixed10-mixture` | 0.00 [0.00, 0.01] | 1100 | 1.000 | - | 0.10 |
| strong | +0.05 | `fixed50-mixture` | 0.00 [0.00, 0.01] | 1200 | 1.000 | - | 0.50 |
| strong | +0.05 | `fixed50-aipw` | 0.34 [0.30, 0.39] | 1200 | 0.980 | - | 0.50 |
| strong | +0.05 | `fixed50-aipw-strata` | 0.70 [0.65, 0.74] | 1250 | 0.995 | - | 0.50 |
| strong | +0.05 | `adaptive-aipw-strata` | 0.64 [0.59, 0.69] | 1300 | 0.988 | - | 0.41 |
| strong | +0.10 | `fixed10-mixture` | 0.02 [0.01, 0.04] | 1550 | 1.000 | - | 0.10 |
| strong | +0.10 | `fixed50-mixture` | 0.12 [0.09, 0.15] | 1700 | 1.000 | - | 0.50 |
| strong | +0.10 | `fixed50-aipw` | 0.83 [0.79, 0.87] | 950 | 0.980 | - | 0.50 |
| strong | +0.10 | `fixed50-aipw-strata` | 0.99 [0.98, 1.00] | 750 | 0.993 | - | 0.50 |
| strong | +0.10 | `adaptive-aipw-strata` | 1.00 [0.99, 1.00] | 750 | 0.978 | - | 0.26 |

## What this shows

- **The mixture prior is predeclared.** Both beta-mixture designs use `99` observations per arm to set the prior concentration, selected from the planned +20-point target effect and 50% baseline. Observed outcomes never choose the horizon.
- **Empirical coverage clears 95%.** The true effect stayed inside the interval path at every checkpoint in at least 96.75% of runs across all 45 cells. Under the null, the highest observed any-time false-positive rate was 3.0%. The AIPW confidence sequence is asymptotic, so these results do not establish a finite-sample anytime-valid guarantee for AIPW designs.
- **The +5-point target is not met.** By 2,000 occasions, AIPW power was 0.30–0.33 with no predictive stratum (`flat`), 0.39–0.46 with moderate strata, and 0.64–0.70 for the strong-strata models. The best cell remains below 80%. A fixed-sample 50/50 test at a 50% baseline needs about 3,140 occasions for 80% power.
- **Adaptive allocation helps prove +10-point effects.** It detected 95.8%–99.8% of +10-point runs by 2,000 occasions, with median detection at 750–800 occasions, while withholding the memory on 25–29% of occasions. The shipped 10% holdout mixture design detected 2.0%–7.5%.

## Reproduce

    python -m benchmarks.adaptive_allocation_bench --reps 400 --out adaptive-allocation.json
