from __future__ import annotations

import random
import shutil
import tempfile

from commontrace import experiment, holdout_io, integrity
from commontrace.commands import experiment_cmd

SEEDS = list(range(30))
PER_ARM = 150
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
    refused = 0
    for seed in SEEDS:
        root = _fresh_root()
        try:
            config = holdout_io.configure(root, rate=0.1)
            rng = random.Random(seed)
            for i in range(50):
                occasion = f"occ-{i}"
                rate = 0.1 if i % 2 == 0 else 0.5
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
