"""One command that runs CommonTrace's causal-measurement claim against
seeded ground truth: does a real effect get detected, does a null effect
stay unclaimed, and does a tampered log get refused rather than scored?

WHY THIS EXISTS
---------------
STRATEGY.md and README.md describe a specific claim: randomized-holdout
measurement recovers a real effect on a customer's own data, and refuses
to report a number when the experiment that would produce it is
compromised. Both halves are already covered by unit tests
(tests/test_experiment.py, tests/test_integrity.py) that pin specific,
hand-built fixtures. Neither answers the question a technical buyer
actually asks -- "run it yourself, with seeds you did not pick, and show
me it still holds" -- which is what this script is for.

    python -m commons.eval.causal_harness

Every scenario below drives the SAME functions the CLI does:
`holdout_io.assign_and_log`/`record_outcome` (what `commontrace query
--experiment` and `commontrace capture --occasion-id` write), then
`experiment_cmd._load`/`scope_to_current_salt` and `integrity.audit` (what
`commontrace experiment` reads) -- not a re-implementation of either. If
this script and the CLI ever disagreed, that would be this repository's
own bug, not a simulation artifact.

WHAT THIS DOES NOT DO
------------------------
Compare against any other vendor, or claim a number for anything this
product does not itself compute. Each scenario reports the fraction of
SEEDS (chosen once, in this file, before any run) for which the correct
verdict came back -- a rate, not a single pass/fail draw, so the number is
not a coin flip dressed up as a demonstration.
"""
from __future__ import annotations

import random
import shutil
import tempfile

from commontrace import experiment, holdout_io, integrity
from commontrace.commands import experiment_cmd

#: Chosen once, here, before any scenario ran -- not tuned afterward to make
#: a rate look better. 30 is enough for a stable percentage without making
#: this script slow to re-run.
SEEDS = list(range(30))
PER_ARM = 150
#: A scenario is reported PASS when at least this fraction of seeds agree.
#: Not 100%: a real +35pp effect at n=150/arm is well-powered but a
#: two-sided test still has a small chance of an underpowered-looking draw,
#: and pretending otherwise would be a different kind of dishonesty than
#: the one this script exists to avoid.
PASS_THRESHOLD = 0.90


def _fresh_root() -> str:
    return tempfile.mkdtemp(prefix="commontrace-causal-harness-")


def _analyze(root: str) -> tuple[integrity.IntegrityReport, dict[str, experiment.CausalEffect]]:
    all_rows, _rate, _corrupt = experiment_cmd._load(root)
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, all_rows)
    report = integrity.audit(rows)
    effects = {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}
    return report, effects


def _seeded_run(root: str, *, true_effect: float, baseline: float, seed: int, rate: float = 0.5) -> None:
    """Log PER_ARM occasions for lesson "L" under a real holdout, where the
    ground truth (which arm actually changes the outcome) is decided HERE,
    by this script, not read back from anything commontrace computed."""
    config = holdout_io.configure(root, rate=rate)
    rng = random.Random(seed)
    for i in range(PER_ARM):
        occasion = f"occ-{i}"
        withheld = holdout_io.assign_and_log(
            root, ["L"], occasion_id=occasion, rate=rate, salt=config.salt,
        )
        p = baseline if "L" in withheld else baseline + true_effect
        holdout_io.record_outcome(root, occasion, rng.random() < p)


def scenario_detects_a_real_effect() -> dict:
    """A lesson with a genuine +35pp effect should be called HELPS, across
    seeds nobody chose to be favorable."""
    hits, recovered = 0, []
    for seed in SEEDS:
        root = _fresh_root()
        try:
            _seeded_run(root, true_effect=0.35, baseline=0.45, seed=seed)
            _report, effects = _analyze(root)
            row = effects.get("L")
            if row is not None and row.verdict == experiment.VERDICT_HELPS:
                hits += 1
                recovered.append(row.effect)
        finally:
            shutil.rmtree(root, ignore_errors=True)
    detail = f"mean recovered effect {sum(recovered) / len(recovered):.1%}" if recovered else "never recovered"
    return {"name": "detects a real +35pp effect as HELPS", "rate": hits / len(SEEDS), "detail": detail}


def scenario_does_not_claim_a_null_effect() -> dict:
    """A lesson with NO true effect must never be reported HELPS or HURTS --
    it should read NO_EFFECT or UNDERPOWERED instead."""
    correct = 0
    for seed in SEEDS:
        root = _fresh_root()
        try:
            _seeded_run(root, true_effect=0.0, baseline=0.5, seed=seed)
            _report, effects = _analyze(root)
            row = effects.get("L")
            if row is None or row.verdict not in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS):
                correct += 1
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return {
        "name": "never claims an effect for a null lesson",
        "rate": correct / len(SEEDS),
        "detail": f"{len(SEEDS) - correct}/{len(SEEDS)} seed(s) falsely called HELPS/HURTS",
    }


def scenario_refuses_a_tampered_log() -> dict:
    """Two different holdout rates recorded under ONE salt -- the exact
    failure `holdout_io.ExperimentConfig`'s own docstring names (two
    retrievers disagreeing about the rate) -- must be reported
    unreadable/COMPROMISED, never scored as if it were a clean run.

    The salt has to be the store's OWN configured salt, not an invented
    string: `experiment_cmd.scope_to_current_salt` -- what `commontrace
    experiment` itself scopes every analysis through -- filters every row
    to `holdout_io.load_config(root).salt` before anything else runs, so a
    row logged under a salt this store never configured would simply be
    filtered out before `check_assignment_drift` ever saw it, and this
    scenario would report a false PASS for the wrong reason (nothing
    analysed, rather than something analysed and correctly refused). This
    was reproduced, not assumed: the first version of this scenario used
    an invented salt and scored 0/30 for exactly that reason.
    """
    refused = 0
    for seed in SEEDS:
        root = _fresh_root()
        try:
            config = holdout_io.configure(root, rate=0.1)
            rng = random.Random(seed)
            for i in range(50):
                occasion = f"occ-{i}"
                rate = 0.1 if i % 2 == 0 else 0.5  # the tamper: disagreeing rate, same (real) salt
                holdout_io.assign_and_log(root, ["L"], occasion_id=occasion, rate=rate, salt=config.salt)
                holdout_io.record_outcome(root, occasion, rng.random() < 0.5)
            report, _effects = _analyze(root)
            if not report.readable:
                refused += 1
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return {
        "name": "refuses a log with two holdout rates under one salt",
        "rate": refused / len(SEEDS),
        "detail": "fraction correctly reported unreadable/COMPROMISED",
    }


SCENARIOS = (
    scenario_detects_a_real_effect,
    scenario_does_not_claim_a_null_effect,
    scenario_refuses_a_tampered_log,
)


def main() -> int:
    print(f"commontrace causal-detection harness -- {len(SEEDS)} seeds per scenario, PASS at >={PASS_THRESHOLD:.0%}\n")
    all_ok = True
    for scenario in SCENARIOS:
        result = scenario()
        ok = result["rate"] >= PASS_THRESHOLD
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {result['name']}: {result['rate']:.0%} -- {result['detail']}")
    print()
    print(
        "Reproduce: `python -m commons.eval.causal_harness`. SEEDS is a plain list in "
        "this file, fixed before any run -- change it and re-run to check these numbers "
        "yourself rather than trusting this printout."
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
