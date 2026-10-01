# Causal coverage and automatic withdrawal: measured

Reproduce: `python -m commons.eval.coverage_harness` (200 seeds, every adapter, ~5 min on 3 cores;
`--scenario withdrawal --seeds 100` for the second table). Seeds are `range(n)`, fixed before any run.

Each run drives a vendor-shaped client (the documented return shape, not a live account) through its
adapter, `MeasuredMemory`, the holdout log and the same analysis `commontrace experiment` runs.
Outcomes are drawn by the harness from a planted truth: baseline 50%, one memory +20pp, one −20pp,
one 0, holdout 50%, 400 occasions per run.

## Is the answer right? 200 seeds per adapter

Verdict right = HELPS for +20pp, HURTS for −20pp, neither for 0. CI covers = the 95% interval contains
the planted effect. Acceptance: both at least 90%.

| Adapter | +20pp verdict | +20pp CI | −20pp verdict | −20pp CI | 0pp not claimed | 0pp CI |
|---|---|---|---|---|---|---|
| mem0 | 98% | 96% | 97% | 96% | 96% | 95% |
| letta | 98% | 93% | 97% | 94% | 96% | 96% |
| zep | 97% | 96% | 98% | 92% | 96% | 96% |
| claude-memory-store | 96% | 96% | 98% | 96% | 97% | 97% |
| agentcore | 96% | 95% | 96% | 94% | 94% | 94% |

All five pass. Mean estimates are within about 1pp of the planted effect.

## Does automatic withdrawal act in time? 100 seeds

Policy `withdraw`, a memory that lowers success by 20pp. The verdict acted on is the anytime-valid one,
which stays valid however often it is looked at; that is why it takes longer than a fixed-horizon plan.

| | |
|---|---|
| Harmful memory withdrawn | 100% of runs |
| Occasions to withdraw | median 300, p90 425 |
| Fixed-horizon plan for the same effect | 198 occasions (median is 1.5x) |
| Helpful memory withdrawn | 0% |
| Harmless memory withdrawn | 0% |

## Limits

* Planted effects are one size (20pp) at one baseline (50%); smaller effects take proportionally more
  occasions, which `commontrace function forecast` says up front.
* The fakes check the adapter's handling of each SDK's documented shape; they do not exercise a live
  account, a network or a vendor's ranking.
* Memories are independent here. Memories that are always delivered together cannot be separated by
  any holdout; the experiment's overlap report says so.
* The evidence is re-read every 25 recalls by default (`check_every`), so a withdrawal can land up to
  that many occasions after the verdict first holds.
