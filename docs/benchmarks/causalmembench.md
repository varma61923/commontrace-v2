# CausalMemBench

Recall benchmarks ask whether the right passage was found. CausalMemBench asks
what a production fleet pays for: once task outcomes start arriving (late, noisy,
some never), does the memory system stop delivering memories that make outcomes
worse, without throwing away the ones that help?

Every memory's true effect is seeded, so each policy is scored against ground
truth. The default world is confounded on purpose:

- `help-hard` truly helps (+20 points) but is only eligible on hard occasions
  (25% base success), so the occasions it is delivered on succeed *less* often
  than average.
- `harm-easy` truly hurts (-15 points) but is only eligible on easy occasions
  (65% base success), so its occasions succeed *more* often than average.
- Three more helpful memories (+15), eight neutral ones and two more harmful ones
  (-15) are eligible on randomly chosen occasions.
- Outcomes arrive 50 occasions late, and 10% are never reported.

## Result (10 seeds x 6,000 occasions)

| policy | success | regret vs oracle | harmful deliveries | harmful withdrawn | false withdrawals / run | runs losing a helpful memory |
| --- | --: | --: | --: | --: | --: | --: |
| deliver-all | 56.4% ± 0.5% | 23.4% ± 0.5% | 9821.2 ± 64.6 | 0.0% | 0.0 | 0/10 |
| correlational | 67.3% ± 3.4% | 12.4% ± 3.2% | 3404.3 ± 1535.3 | 73.3% ± 14.1% | 2.3 ± 1.5 | **10/10** |
| commontrace (10% holdout) | 60.6% ± 4.0% | 19.2% ± 3.8% | 7048.6 ± 1681.4 | 50.0% ± 23.6% | 0.0 | 0/10 |
| commontrace@0.3 | 63.2% ± 3.5% | 16.6% ± 3.4% | 3628.3 ± 1411.0 | 100.0% | 0.0 | 0/10 |
| commontrace@0.3+graduate | 65.9% ± 4.7% | 13.9% ± 4.6% | 3738.8 ± 1606.0 | 93.3% ± 14.1% | 0.0 | 0/10 |
| commontrace@0.5+graduate | 66.2% ± 5.0% | 13.6% ± 5.0% | 2706.8 ± 1023.0 | 100.0% | 0.0 | 0/10 |
| oracle | 79.8% ± 0.4% | 0.0% | 0 | 100.0% | 0.0 | 0/10 |

What it shows, and what it does not:

- **The correlational heuristic withdraws a truly helpful memory in every run**
  (`help-hard`, and often a neutral one). Its success rate is within noise of the
  best CommonTrace setting, but the loss is invisible in production: nothing
  distinguishes a wrongly withdrawn memory from a rightly withdrawn one.
- **CommonTrace never withdrew a memory that does not hurt** in any run, at any
  holdout rate. That is the anytime-valid verdict doing its job.
- **The default 10% holdout is slow** at this effect size and horizon: it found
  half the harmful memories within 6,000 occasions. A 30-50% holdout found all of
  them. Use `commontrace experiment --plan` to size the rate for your volume.
- **Graduation** (`CausalMemory(graduate=True)`) stops randomizing a memory once it
  is proven to help, recovering most of the cost of the holdout.
- This is a seeded simulation of the measurement-and-withdrawal layer. It says
  nothing about any system's recall quality or live business value.

## Reproduce

```bash
python -m benchmarks.causalmembench --seeds 10 \
  --policy deliver-all --policy correlational --policy commontrace \
  --policy commontrace@0.3 --policy commontrace@0.3+graduate \
  --policy commontrace@0.5+graduate --policy oracle --out causalmembench.json
```

The holdout salt is derived from the seed, so runs are byte-for-byte
reproducible. The JSON report records the scenario, every per-seed run and the
benchmark source hash.

## Test your own system

Pass `--policy module:factory`. The factory receives the memory list (and `seed=`
if it accepts it) and returns an object with:

- `deliver(occasion_id, eligible_ids) -> list[str]`: which eligible memories to deliver;
- `observe(occasion_id, succeeded)`: an outcome, arriving late or never;
- `withdrawn() -> set[str]`: memories the policy has stopped delivering.

Any memory product that learns from feedback (edge weights, utility scores,
reflection) can be wrapped this way and scored on the same world.
