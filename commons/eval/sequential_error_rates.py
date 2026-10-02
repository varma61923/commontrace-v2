"""What repeated looks at a running experiment actually cost, measured."""

from __future__ import annotations

import random

from commontrace import experiment

PER_ARM = 500
TRIALS = 300
BASELINE = 0.5
EFFECT = 0.10

FIRST_LOOK = 50
LOOK_EVERY = 25


def _run(true_effect: float, *, seed: int, sequential: bool) -> float:
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
