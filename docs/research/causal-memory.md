# Measuring What Agent Memory Is Worth: Randomized, Anytime-Valid Evaluation of Memories in Production

*Working draft. Every number below is produced by a script in this repository and
is reproducible with the command given beside it. None is a measurement of a
live deployment.*

## Abstract

Agent memory systems are evaluated by whether they retrieve the right passage.
Operators need a different answer: did delivering this memory change how the
task went? Retrieval benchmarks cannot answer it, and correlational signals
from production answer it wrongly, because the occasions on which a memory is
eligible differ systematically from those on which it is not. We describe
CommonTrace, a memory layer that randomizes every memory's delivery per
occasion with a deterministic, published hash; reads each memory's effect with
a confidence sequence that stays valid however often it is checked; withdraws
memories proven to hurt; binds every record to the authority of whoever wrote
it; and packages the evidence so a third party can recompute every verdict. In
a confounded simulation where a correlational policy withdrew a truly helpful
memory in 10 of 10 runs, the randomized policy withdrew no memory that does
not hurt, at any holdout rate. Adaptive allocation in published eras, read by
an augmented inverse-propensity-weighted (AIPW) confidence sequence, kept
time-uniform coverage at or above 96.7% in all 45 simulated cells and detected
a +10-point memory in 96-100% of runs by 2,000 occasions, against 2-7% for a
fixed 10% holdout, while withholding the memory half as often as a fixed 50%
holdout. A +5-point effect within 2,000 occasions at 80% power was not reached
(best 0.70), and we explain why no anytime-valid design can reach it at a 50%
baseline without a strongly predictive covariate.

## 1. Introduction

Memory is now standard in coding, support and embodied agents. Its evaluation
has not kept pace with its deployment. Recall@k and answer accuracy on
long-conversation benchmarks measure retrieval quality on a fixed question set;
they say nothing about whether a particular stored lesson ("retry payments with
an idempotency key", "cut through aisle 7") improves or degrades the outcomes of
the occasions it is delivered on. Production telemetry seems to offer that
signal, but it is confounded: a lesson that is only retrieved on hard tasks is
associated with failure even when it helps.

The remedy is old and well understood in medicine and online experimentation:
randomize. CommonTrace withholds each eligible memory on a small, deterministic
share of occasions and compares outcomes. Three problems make this less simple
than an A/B test:

1. **Repeated looks.** Operators watch dashboards continuously and act on what
   they see. A fixed-sample test read continuously crosses its threshold by
   chance; we measured this at roughly one in three null runs for the naive
   reading (`commons/eval/sequential_error_rates.py`). Verdicts must be
   anytime-valid.
2. **Cost.** Withholding a memory that helps costs outcomes. The rate that makes
   measurement fast makes it expensive; the rate that makes it cheap makes it
   slow.
3. **Trust.** A vendor's claim that its memory helps is worth little unless the
   buyer can recompute it, and unless the memory itself cannot be poisoned by
   whoever can write to it.

## 2. Design

**Assignment (PROTOCOL 13.1).** Memory `m` is withheld on occasion `o` in
experiment `s` iff `u(s, m, o) < r`, where `u` is a BLAKE2b-derived uniform and
`r` the holdout rate. Assignment depends on nothing else, so any party with the
salt can re-derive every arm. Outcomes are reported later under the same
occasion id, possibly never; the integrity audit checks differential attrition,
arm balance, re-randomization, conflicting arms and censoring before any
effect is reported.

**Verdicts.** Each memory receives HELPS, HURTS, NO_MEASURABLE_EFFECT (the
sample could have detected a practically relevant effect and did not) or
UNDERPOWERED (it could not yet). The shipped interval is a beta-binomial mixture
confidence sequence per arm, combined by Bonferroni and Minkowski difference:
exact and time-uniform.

**Harm policy.** With `on_harm=withdraw`, a memory whose interval lies wholly
below zero stops being delivered at the next recall.

**Adaptive allocation (PROTOCOL 13.5).** Schedules set each memory's rate per
era: 50% while unproven, 5% once proven to help or proven to be negligible,
95% withheld once proven to hurt. A schedule is computed only from outcomes
recorded before it, is hash-chained to its predecessor, and takes effect a few
seconds after it is written, so every assignment can be checked against the
schedule in force when it was made. Every assignment logs the rate it was
compared against.

**Estimation under adaptive rates.** With injection probability `pi = 1 - r`
and running arm means `m1, m0` computed from earlier occasions only, the
pseudo-outcome

    phi = m1 - m0 + Z (Y - m1) / pi - (1 - Z) (Y - m0) / (1 - pi)

has conditional mean equal to the effect whatever the history, so the `phi`
form a martingale-difference sequence. We read their mean with the asymptotic
confidence sequence of Waudby-Smith, Arbour, Sinha, Kennedy and Ramdas
(Annals of Statistics, 2024), with the mixture precision fixed in advance by
the planned horizon, and report its running intersection: a time-uniform
sequence covers at every time at once, so the intersection covers too, and a
verdict is not undone when a later era's lower propensities make the scores
noisier. Optional strata (a short pre-treatment label) let the running means
condition on the occasion.

**Authority.** Every record is bound at write time to its authenticated writer
and signed (Ed25519). Derived records inherit the least trusted source, so a
trusted summarizer cannot launder an external claim. An operator can require a
minimum authority per action.

**Proof packages.** A package contains the raw assignments, the analysis, the
allocation schedules and an Ed25519 signature over the value ledger. A verifier
recomputes every estimate, the integrity verdict and the ledger from the raw
rows and fails on any difference.

## 3. Experiments

### 3.1 Confounded withdrawal (CausalMemBench)

Fifteen memories with seeded effects; the strongest helpful memory is
eligible only on hard occasions and the strongest harmful one only on easy
ones. Outcomes arrive 50 occasions late and 10% never arrive. Ten seeds of 6,000
occasions (`python -m benchmarks.causalmembench --seeds 10 ...`).

| Policy | Success | Harmful withdrawn | Runs losing a helpful memory |
| --- | --: | --: | --: |
| Deliver everything | 56.4% | 0% | 0/10 |
| Correlational withdrawal | 67.3% | 73.3% | **10/10** |
| Randomized, 10% holdout | 60.6% | 50.0% | 0/10 |
| Randomized, 50% holdout + graduation | 66.2% | 100% | 0/10 |
| Oracle | 79.8% | 100% | 0/10 |

The correlational policy's success rate is within noise of the best randomized
one, but it withdrew the most valuable memory in every run, and nothing in its
own telemetry reveals that. The randomized policy never withdrew a memory that
does not hurt.

### 3.2 Adaptive allocation (target E)

One memory with a true effect of 0, +5 or +10 points; occasions fall in strata
with base rates of 50% (`flat`), 20/50/80% (`moderate`) or 5/50/95% (`strong`).
400 runs per cell, 2,000 occasions, intervals checked every 50 occasions
(`python -m benchmarks.adaptive_allocation_bench --reps 400`; full table in
`docs/benchmarks/adaptive-allocation.md`).

| Strata | Effect | Fixed 10%, mixture | Fixed 50%, mixture | Fixed 50%, AIPW + strata | Adaptive, AIPW + strata | Withheld (adaptive vs fixed 50%) |
| --- | --: | --: | --: | --: | --: | --: |
| flat | +10 | 0.07 | 0.28 | 0.94 | 0.96 | 0.29 vs 0.50 |
| moderate | +10 | 0.07 | 0.28 | 1.00 | 0.99 | 0.25 vs 0.50 |
| strong | +10 | 0.02 | 0.12 | 0.99 | 1.00 | 0.26 vs 0.50 |
| flat | +5 | 0.00 | 0.00 | 0.30 | 0.33 | 0.45 vs 0.50 |
| strong | +5 | 0.00 | 0.00 | 0.70 | 0.64 | 0.41 vs 0.50 |

Power is the share of runs detected by 2,000 occasions. Time-uniform coverage
was at least 96.7% in every one of the 45 cells, and with no effect an interval
excluded zero at any checkpoint in at most 3.0% of runs.

Two findings. First, most of the gain comes from the estimator, not from the
allocation: at the same 50% rate the AIPW sequence detects a +10-point memory
in 83-94% of runs where the beta-mixture interval detects it in 12-28%.
Second, adaptive allocation keeps that power while halving how often a proven
memory is withheld. It does not make a +5-point effect detectable within 2,000
occasions: at a 50% baseline a fixed-sample test needs 3,140 occasions for 80%
power, and an interval valid at every look is necessarily wider. Only a
covariate that predicts the outcome strongly narrows it enough (0.64-0.70 with
the `strong` strata).

### 3.3 Memory poisoning (PoisonBench)

Nine attacks through the real write and recall paths for the action `payment`,
five phrasings each (`python -m benchmarks.poisonbench --variants 5`). Without
an authority policy every origin attack (external document, MINJA-style agent
write, tool echo, summary laundering, self-corroboration, salami fragments)
succeeded in 100% of phrasings; with `payment: user` required, none did, at
100% clean utility. Forged and stripped receipts fail verification with or
without a policy.

### 3.4 Multi-principal governance (GovBench)

A hospital ward with five principals and scoped records, recalled through three
read paths: 100% utility, 0 leaks in 69 checks before forgetting and 48 after,
0 forgotten records delivered in 36 checks; the forgetting certificate follows
lineage to a derived summary (`python -m benchmarks.govbench`).

### 3.5 An embodied fleet

Four simulated and two real warehouse robots through the real gateway, 1,200
occasions per environment (`python -m benchmarks.robot_fleet_demo`). A shortcut
harmless in simulation and harmful on the floor was read as HURTS on the real
fleet, -0.186 [-0.328, -0.037] against a true -0.200; merging simulated with
real logs was refused; the withdrawing gateway then delivered it 0 times in 100
occasions while the protected safety memory was delivered every time. Recall
p95 through the gateway was 0.75 ms.

### 3.6 Retrieval quality

Measurement does not replace retrieval. On full LoCoMo (1,540 questions) the
share of gold evidence turns delivered at a 1,500-token budget is 78.69%
lexical, 82.00% dense and 83.90% dense with a cross-encoder; with a budget sized
by the question's shape it is 83.73% dense. A 0.5B local reader answers 4% of a
100-question subset without memory, 19% with lexical memory and 26% with dense
adaptive memory, graded by a deterministic containment judge.

## 4. Limitations

All results are seeded simulations or offline benchmarks on one 4-core
machine. The AIPW sequence's coverage is asymptotic; we measured it from 100
occasions on and it held in every cell, but it is not an exact guarantee at
very small samples, where the shipped mixture interval remains in use for
unscheduled experiments. Randomization withholds memories; operators must
accept that cost, which adaptive allocation reduces but does not remove. The
authority layer assumes the operator and the signing keys are not compromised.
Model-graded answer accuracy with a frontier reader was not available in this
environment.

## 5. Reproducibility

Every table names its command. Benchmark reports record their source SHA-256
and seed; holdout salts derive from the seed, so runs are byte-for-byte
reproducible. The protocol (`protocol/PROTOCOL.md`) and its conformance vectors
(`protocol/conformance/vectors.json`, including 40 allocation vectors) let an
independent implementation check itself against the reference.
