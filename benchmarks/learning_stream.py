"""Reproducible chronological simulation of the production learning contract.

This is a regression experiment, not an external leaderboard or robot hardware
result. No training observation can use an evaluation task. The same task draws
compare absent, frozen and adaptive memory; context shifts reverse useful advice.
Use existing conversation/vendor benchmarks for model-backed comparisons.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from unimem import learning_contract as contract

REFERENCES = {
    "CoALA": "https://arxiv.org/abs/2309.02427",
    "MemGPT": "https://arxiv.org/abs/2310.08560",
    "A-MEM": "https://arxiv.org/abs/2502.12110",
    "Mem0": "https://arxiv.org/abs/2504.19413",
    "Continual World": "https://arxiv.org/abs/2105.10919",
}
KINDS = ("helpful", "neutral", "harmful")
COHORTS = ("agent", "llm", "robot-sim")


def _draw(seed: int, identity: str) -> float:
    return int.from_bytes(hashlib.sha256(f"{seed}:{identity}".encode()).digest()[:8], "big") / 2**64


def _reward(kind: str, injected: bool, shifted: bool) -> float:
    direction = {"helpful": 1, "neutral": 0, "harmful": -1}[kind]
    if shifted:
        direction *= -1
    return 0.5 + (0.4 * direction if injected else 0)


def _measurement(config, occasion, cohort, value, seed):
    if cohort == "agent":
        return {"occasion_id": occasion, "succeeded": _draw(seed, "outcome:" + occasion) < value}
    if cohort == "llm":
        return {"occasion_id": occasion, "reward": value}
    # Completion is simulated telemetry; no robot actions are issued.
    return {"occasion_id": occasion, "metrics": {"completion": value}}


def run(*, seed: int = 23, train_per_group: int = 1200, eval_per_group: int = 300) -> dict:
    if type(train_per_group) is not int or not 10 <= train_per_group <= 10000:
        raise ValueError("train_per_group must be from 10 to 10000")
    if type(eval_per_group) is not int or not 1 <= eval_per_group <= 10000:
        raise ValueError("eval_per_group must be from 1 to 10000")
    all_decisions: list[dict] = []
    results: list[dict] = []
    digest_inputs: list[str] = []
    for cohort in COHORTS:
        config = {
            **contract.validate_config(
                {
                    "mode": "adaptive",
                    "utility": {"terms": {"completion": {"weight": 1, "low": 0, "high": 1, "better": "higher"}}},
                }
            ),
            "experiment_id": f"simulation-{seed}-{cohort}",
            "revision": 1,
            "salt": f"simulation-{seed}-{cohort}",
        }
        evidence: list[dict] = []
        policy: dict[str, dict] = {}
        for phase, shifted in [("initial", False), ("shifted", True)]:
            context = cohort + "-" + phase
            for index in range(train_per_group):
                for kind in KINDS:
                    occasion = f"train-{context}-{kind}-{index}"
                    row = contract.make_assignment(
                        config,
                        occasion,
                        [{"id": kind, "revision": 1, "text": kind}],
                        task_context=context,
                        policy=policy,
                    )
                    row["policy_revision"] = 0
                    row["delivery"] = row["inject"][:]
                    value = _reward(kind, bool(row["inject"]), shifted)
                    row["outcome"] = contract.normalize_outcome(
                        config, occasion, _measurement(config, occasion, cohort, value, seed)
                    )
                    evidence.append(row)
            # One predeclared look per phase; later evaluation never enters evidence.
            report = contract.evaluate(config, evidence)
            for decision in report["decisions"]:
                if decision["verdict"] in ("HELPS", "HURTS", "NO_MEASURABLE_EFFECT"):
                    policy[decision["key"]] = {"rate": decision["rate"], "disabled": decision["disabled"]}
            all_decisions.extend(
                {**d, "cohort": cohort, "phase": phase} for d in report["decisions"] if d["task_context"] == context
            )
            # Evaluate on disjoint future occasions with common draws for all variants.
            scores: dict[str, list[float]] = {variant: [] for variant in ("absent", "frozen", "adaptive")}
            for index in range(eval_per_group):
                for kind in KINDS:
                    occasion = f"eval-{context}-{kind}-{index}"
                    assigned = contract.make_assignment(
                        config,
                        occasion,
                        [{"id": kind, "revision": 1, "text": kind}],
                        task_context=context,
                        policy=policy,
                    )
                    for variant in scores:
                        injected = variant == "frozen" or (variant == "adaptive" and bool(assigned["inject"]))
                        value = _reward(kind, injected, shifted)
                        score = float(_draw(seed, "outcome:" + occasion) < value) if cohort == "agent" else value
                        scores[variant].append(score)
            results.append(
                {
                    "cohort": cohort,
                    "phase": phase,
                    "tasks": eval_per_group * len(KINDS),
                    **{variant: round(sum(values) / len(values), 6) for variant, values in scores.items()},
                }
            )
        digest_inputs.append(contract.digest(evidence))
    return {
        "schema": "unimem.learning-simulation.v1",
        "simulated": True,
        "seed": seed,
        "train_per_group": train_per_group,
        "eval_per_group": eval_per_group,
        "evidence_digest": contract.digest(digest_inputs),
        "results": results,
        "decisions": all_decisions,
        "references": REFERENCES,
    }


def check(result: dict) -> None:
    for row in result["results"]:
        if row["adaptive"] <= max(row["absent"], row["frozen"]) + 0.025:
            raise AssertionError(f"adaptive regression for {row['cohort']}/{row['phase']}")
    for cohort in COHORTS:
        for phase in ("initial", "shifted"):
            decisions = {d["id"]: d for d in result["decisions"] if d["cohort"] == cohort and d["phase"] == phase}
            helpful, harmful = ("helpful", "harmful") if phase == "initial" else ("harmful", "helpful")
            if decisions.get(helpful, {}).get("verdict") != "HELPS":
                raise AssertionError(f"helpful memory not identified: {cohort}/{phase}")
            if decisions.get(harmful, {}).get("verdict") != "HURTS":
                raise AssertionError(f"harmful memory not identified: {cohort}/{phase}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--train-per-group", type=int, default=1200)
    parser.add_argument("--eval-per-group", type=int, default=300)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = run(seed=args.seed, train_per_group=args.train_per_group, eval_per_group=args.eval_per_group)
    if args.check:
        check(result)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
