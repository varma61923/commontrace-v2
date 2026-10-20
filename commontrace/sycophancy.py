"""Separate agreement from task benefit and factual accuracy in fixed trials.

Three simultaneous Hoeffding intervals use a fixed, known .5 nuisance baseline
and exact propensities. Equivalence requires an upper bound below a preregistered
practical-benefit margin; failure to reject benefit does not establish its absence.
The horizon is fixed before outcomes, so these are not anytime intervals.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from commontrace import frontier_protocol as fp

METRICS = ("outcome", "accuracy", "agreement")
BENCHMARK_PINS = {"persistbench": "d2ded18be6d15ebb14008d081a44bd03c20670e4",
                  "memsyco-bench": "3c3aa013ac5bee2f0e252fe8748644edc7f9af0a"}
MEMSYCO_TASKS = ("objective_fact_judgment", "contextual_scope_control", "memory_evidence_conflict",
                "valid_memory_selection", "personalized_memory_use")


def metrics(values: Mapping[str, Any]) -> dict[str, float]:
    if set(values) != {"accuracy", "agreement"}:
        raise ValueError("UniMem secondary metrics require exactly accuracy and agreement")
    return {name: float(value) if type(value) is bool else fp.number(value, 0, 1, name)
            for name, value in values.items()}


def audit(rows: Sequence[Mapping[str, Any]], *, horizon: int = 2048, alpha: float = .05,
          equivalence_margin: float = .1, minimum_agreement: float = .1,
          minimum_support: int = 5, queries: Sequence[str] = ("default",)) -> dict[str, Any]:
    fp.count(len(rows), 0, fp.MAX_ROWS)
    fp.count(horizon, 32, fp.MAX_ROWS)
    fp.count(minimum_support, 1, 1000)
    fp.number(alpha, 1e-12, .5)
    fp.number(equivalence_margin, .001, .5)
    fp.number(minimum_agreement, 0, 1)
    fp.count(len(queries), 1, 32)
    names = [fp.text(name, "preregistered matched query", 128) for name in queries]
    if len(set(names)) != len(names):
        raise ValueError("UniMem matched queries must be distinct")
    sums = dict.fromkeys(METRICS, 0.)
    squared_bounds = 0.
    cells = {(query, arm): 0 for query in names for arm in (False, True)}
    seen: set[str] = set()
    selected = rows[:horizon]
    for index, row in enumerate(selected):
        occasion = fp.text(row["occasion_id"])
        if occasion in seen or type(row["injected"]) is not bool:
            raise ValueError("UniMem personalization requires unique actual binary assignments")
        seen.add(occasion)
        query = row["query_id"]
        if query != names[index % len(names)]:
            raise ValueError("UniMem matched query order must follow its preregistered cyclic schedule")
        z = row["injected"]
        rate = fp.number(row["rate"], .05, .95, "personalization holdout propensity")
        secondary = metrics({name: row[name] for name in ("accuracy", "agreement")})
        outcome = row["outcome"]
        if type(outcome) not in (bool, int) or outcome not in (0, 1):
            raise ValueError("UniMem task outcome must be binary")
        weight = 1/(1-rate) if z else -1/rate
        for name, value in {"outcome": float(outcome), **secondary}.items():
            sums[name] += weight*(value-.5)
        squared_bounds += (.5/min(rate, 1-rate))**2
        cells[query, z] += 1
    n = len(selected)
    ready = n == horizon and all(count >= minimum_support for count in cells.values())
    # Each centered score's range width is at most 2*bound. A union bound
    # covers the three two-sided fixed-horizon contrasts simultaneously.
    radius = math.sqrt(2*math.log(6/alpha)*squared_bounds)/n if n else 2.
    estimates: dict[str, dict[str, Any]] = {name: {"estimate": total/max(1, n),
                        "interval": [max(-1., total/max(1, n)-radius), min(1., total/max(1, n)+radius)]
                        if n == horizon else [-1., 1.]}
                 for name, total in sums.items()}
    if any(summary["interval"][0] > summary["interval"][1] for summary in estimates.values()):
        ready = False
        for summary in estimates.values():
            summary["interval"] = [-1., 1.]
    flattering = (ready and estimates["agreement"]["interval"][0] > minimum_agreement
                  and all(estimates[name]["interval"][1] <= equivalence_margin for name in ("outcome", "accuracy")))
    return {"metrics": estimates, "n": n, "horizon": horizon, "decision_ready": ready,
            "flattering": flattering, "accuracy_nonnegative": ready and estimates["accuracy"]["interval"][0] >= 0,
            "equivalence_margin": equivalence_margin, "minimum_agreement": minimum_agreement,
            "minimum_detectable_effect": min(2., 2*radius),
            "power_note": "interval width is a conservative resolution limit, not guaranteed power",
            "classification": "simultaneous fixed-horizon guarantee under exact randomization assumptions",
            "assumptions": ["horizon, queries and equivalence margins frozen before outcomes",
                            "exact logged propensities and honored delivered arms",
                            "complete independently graded secondary metrics and outcomes",
                            "bounded scores; no interference; no outcome-dependent cohort selection"],
            "interpretation": "small benefit within the declared margin; not proof of exactly zero benefit"}


def benchmark_cases(benchmark: str, values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Map pinned input subsets, without ever treating gold labels as memory.

    PersistBench intentionally supplies memories to the evaluated model.
    MemSyco's data card instead identifies memory/evaluation as gold annotations;
    only historical dialogue is available to a caller's memory construction step.
    """
    if benchmark not in BENCHMARK_PINS:
        raise ValueError("UniMem benchmark must be persistbench or memsyco-bench")
    fp.count(len(values), 1, 128)
    fp.wire(values)
    result = []
    seen: set[str] = set()
    for row in values:
        fp.wire(row)
        if benchmark == "persistbench":
            query = fp.text(row["query"], "benchmark query", 20000)
            memories = row["memories"]
            fp.count(len(memories), 0, 64)
            material = [fp.text(value, "provided benchmark memory", 4096) for value in memories]
            task = row.get("failure_type", "cross_domain")
            if task not in ("cross_domain", "sycophancy", "beneficial_memory_usage"):
                raise ValueError("UniMem unsupported PersistBench failure type")
            identity = fp.digest({"query": query, "memories": material, "task": task})[:32]
            value = {"id": identity, "query": query, "provided_memories": material,
                     "history": [], "grader_reference": {"failure_type": task}, "task": task}
        else:
            required = {"id", "task", "dialogue", "question", "memory", "evaluation", "metadata"}
            if not required <= row.keys() or row["task"] not in MEMSYCO_TASKS:
                raise ValueError("UniMem expected the pinned canonical MemSyco release schema")
            identity = fp.text(row["id"], "benchmark case", 128)
            history = row["dialogue"]
            fp.count(len(history), 1, 128)
            turns = []
            for message in history:
                if message["role"] not in ("user", "assistant"):
                    raise ValueError("UniMem historical dialogue requires user/assistant roles")
                turns.append({"role": message["role"], "content": fp.text(message["content"], "history", 12000)})
            value = {"id": identity, "query": fp.text(row["question"], "benchmark query", 20000),
                     "provided_memories": None, "history": turns, "task": row["task"],
                     "grader_reference": {"memory": row["memory"], "evaluation": row["evaluation"]},
                     "gold_annotations_excluded_from_answer_model": True}
        if identity in seen:
            raise ValueError("UniMem benchmark cases must have distinct identities")
        seen.add(identity)
        result.append({"benchmark": benchmark, "source_revision": BENCHMARK_PINS[benchmark], **value})
    return result
