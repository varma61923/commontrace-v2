"""What repeated looks at a running experiment actually cost, measured.

WHY THIS EXISTS
---------------
`commontrace/experiment.py` gained sequential analysis (alpha spending plus
an anytime-valid confidence sequence) because a customer watching a running
holdout does not look once at a pre-planned sample size -- they look every
morning, and stop when it turns significant. That procedure does not have
the false-positive rate its p-value claims, and the gap is not small.

The unit tests pin the mechanics (a spending function is monotone, a
confidence sequence widens correctly). They cannot answer the question a
buyer actually asks: *how often does this tell me a useless memory works?*
That needs simulation, which is too slow for a test suite and too important
to leave as an unchecked claim in a document.

So this script measures it, and AUDIT_RESPONSE.md §3.2 cites the numbers it
produces along with the configuration below. Re-run it to check them.

    python -m commons.eval.sequential_error_rates

WHAT IS BEING COMPARED
----------------------
Both arms use the same data, the same looking schedule and the same stopping
rule. The ONLY difference is `sequential=True`, so the gap is attributable to
the correction and to nothing else about the design.

An honest reading has to include the cost. Controlling the error rate under
repeated looks is not free: power at a real +10pp effect drops, because a
procedure that will not cry wolf at an accumulating random walk also waits
longer to call a true one. Reporting the false-positive improvement without
that trade would be the same selective reporting this whole subsystem exists
to prevent.
"""

from __future__ import annotations

import random

from commontrace import experiment

#: A properly powered run for the effect being detected, so "underpowered"
#: is not doing the work that the correction is supposed to do. At a 50%
#: baseline and a +10pp effect, this is roughly the arm size
#: `experiment.plan` asks for.
PER_ARM = 500
TRIALS = 300
BASELINE = 0.5
EFFECT = 0.10

#: Look every 25 occasions per arm, starting at 50. That is roughly "check
#: it each morning" on a fleet doing a few hundred eligible occasions a day
#: -- the realistic schedule, not a worst case constructed to fail.
FIRST_LOOK = 50
LOOK_EVERY = 25


def _run(true_effect: float, *, seed: int, sequential: bool) -> float:
    """Fraction of runs that ever declare a positive effect."""
    rng = random.Random(seed)
    alarms = 0
    for trial in range(TRIALS):
        observations: list = []
        fired = False
        for i in range(PER_ARM):
            observations.append(experiment.HoldoutObservation(
                "L", f"{trial}-{i}-t", True, rng.random() < BASELINE + true_effect))
            observations.append(experiment.HoldoutObservation(
                "L", f"{trial}-{i}-c", False, rng.random() < BASELINE))
            n = i + 1
            if n >= FIRST_LOOK and n % LOOK_EVERY == 0:
                for effect in experiment.analyze(
                    observations, sequential=sequential
                ):
                    if effect.significant and effect.effect > 0:
                        fired = True
                if fired:
                    # Stopping on the first significant look IS the
                    # procedure being measured. A simulation that kept
                    # going would measure a different, more disciplined
                    # customer than the one this correction is for.
                    break
        alarms += fired
    return alarms / TRIALS


def main() -> int:
    print(
        f"per_arm={PER_ARM}, trials={TRIALS}, baseline={BASELINE:.0%}, "
        f"first look at {FIRST_LOOK}, then every {LOOK_EVERY} per arm"
    )
    naive_fp = _run(0.0, seed=1, sequential=False)
    seq_fp = _run(0.0, seed=1, sequential=True)
    seq_power = _run(EFFECT, seed=2, sequential=True)

    print()
    print("Under the NULL (the memory does nothing):")
    print(f"  naive repeated looks   false positive: {naive_fp:6.1%}")
    print(f"  sequential             false positive: {seq_fp:6.1%}")
    print()
    print(f"With a real +{EFFECT:.0%} effect:")
    print(f"  sequential             power:          {seq_power:6.1%}")
    print()
    print(
        "The trade, stated: the correction buys a false-positive rate near "
        "the nominal 5% at the cost of power, because a procedure that will "
        "not cry wolf at an accumulating random walk also waits longer to "
        "call a true effect. An underpowered null is reported as "
        "UNDERPOWERED, never as 'no effect'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
