# Agent Learning Proof: rehearsal

Function: **Easy**. Occasion: one task. Success: the task finished. Window: 0 days.

## Result

1 memory/memories HELP; 1 HURT; 0 show no measurable effect; 1 not yet decided.

## Can this be trusted?

**Sound.** Nothing in the assignment record undermines the comparison below.

990 of 990 assignment(s) have a recorded outcome. Only those enter the comparison.

- **[OK] Both arms are observed at a similar rate (100.0% injected, 100.0% withheld; p=1).**
  Dropping unresolved occasions is unbiased when the two arms lose them equally, which is what this measures.
- **[OK] Reporting schedule not checkable.**
  This log does not carry an assignment time and a resolution time for every row, so how long each occasion was watched is unknown. The attrition check still reads the totals; only the timing does not.
- **[OK] Withheld share is 50.9%, consistent with the configured 50.0% (p=0.589).**
- **[OK] One randomization throughout.**
- **[OK] One retrieval configuration throughout.**
- **[OK] Every (lesson, occasion) sits in exactly one arm.**
- **[OK] All 3 lesson(s) held the same text throughout.**
- **[OK] 486 of 990 recorded occasions succeeded.**
- **[OK] No retrieval scores recorded, so eligibility strength cannot be assessed.**
  Assignments logged before retrieval evidence was recorded carry no relevance score. Newer assignments will carry one; this check reports on those.
- **[OK] No lesson is absorbing a disproportionate share of occasions.**

_Checked here: attrition, arm balance, mid-run re-randomization, conflicting arms, whether the lesson text held still, and whether the outcome varies at all. NOT checkable here: whether an agent used a lesson it was told to withhold. That leaves no trace in the record and biases the effect toward zero -- it is honoured by the client or not at all._

## What each memory did

| Memory | Verdict | Effect | 95% CI | Injected | Withheld |
|---|---|---|---|---|---|
| `sim-harmful-memory` | **HURTS** | -29.1% | [-38.7%, -19.4%] | 168 | 162 |
| `sim-helpful-memory` | **HELPS** | +22.5% | [+12.8%, +32.3%] | 156 | 174 |
| `sim-neutral-memory` | **UNDERPOWERED** | +6.0% | [-4.8%, +16.8%] | 162 | 168 |

_Intervals are anytime-valid: they stay valid however often the run was looked at, which is why they are wider than a single look at a finished run would give._

## Harmful memories

These made outcomes WORSE: `sim-harmful-memory`. The store's policy is `inform`, so they are still being delivered: `commontrace retrieval --on-harm withdraw` stops that.

Over the measured window they cost about **49 occasions**, 95% interval 33 to 65. That is what stopping them gives back; it is measured, not forecast, and it is not billed.

## Value delivered

**-14 occasions** went differently because of this memory, over the measured window (95% CI -36 to +9).

Computed from 2 memory/memories whose causal effect is established. 1 contributed nothing, listed below with why.

_Counting every measured memory rather than only the ones that cleared significance gives -14 occasions. The figure above selects on the same data it reports, which biases its magnitude away from zero; this one does not, and is not billable because it includes effects the experiment did not establish. The gap between them is what that selection is worth._

**Whole-policy comparison:** -1.0% (95% CI -7.3% to +5.2%) across 486 occasions that received a memory against 504 that received none -- -5 occasions.

_Every occasion counts once here, whatever number of memories it received, which is what makes this addable when the per-memory figures are not. It attributes nothing to an individual memory._

| Memory | Verdict | Injected | Effect | Occasions | Counted |
|---|---|---:|---:|---:|---|
| `sim-harmful-memory` | HURTS | 168 | -29.1% | -49 | yes |
| `sim-helpful-memory` | HELPS | 156 | +22.5% | +35 | yes |
| `sim-neutral-memory` | UNDERPOWERED | 162 | +6.0% | +10 | no |

Why the others contributed nothing:

- `sim-neutral-memory` — not established: this design could not detect an effect worth acting on, so multiplying it by a volume would produce a large number with no evidence under it.

> Memories measured as HURTING are **subtracted above, not dropped**. A value figure that sums only the winners is a brochure; this product's claim is that it will tell you when its own memory is making things worse, and a number that quietly excludes those retracts the claim.

## Pre-registration

Registered before the run and run as registered.

- fingerprint `0ef8fc6267a718fe7bbaeb7d5d1b25e323e2d3c36aad44dc2cd2f4b3086d32cd`

## Ledger and signature

No ledger: nothing is billable here (no agreed value per occasion, an unreadable experiment, or nothing established).

Ledger root `19fb4bcc3347bd43084a4915cc877a109486ad877e4f0bfbbb87e833b9c4bb5f`
Data digest `3af331c035fdb29e3829cc1b958a47bb0e6856f67a535d71b23ac37bd66cbea8`

**Not signed.** The chain and the data digest still check; without an issuer signature a party with write access could replace the whole package.

## Check it yourself

`proof.json` is this report in machine-readable form and `assignments.csv` is one row per arm decision, including those that never got an outcome. Run:

    commontrace proof verify <this directory> [--key-file KEY]

It recomputes the audit, every estimate and the value arithmetic from the CSV alone, and checks the digest, the ledger chain, the pre-registration fingerprint and the signature. The digest rule is in `commontrace/raw_export.py` and is short enough to reimplement in any language.

## What this does not show

This report does not show that the agent honoured a memory it was told to withhold (that leaves no trace and biases the effect toward zero), that outcomes were reported honestly, or that the registration predates the data beyond what the assignment log's own timestamps say. A signature authenticates who issued this package, not that the issuer was honest about what it ran; that is what the raw data and `commontrace proof verify` are for.
